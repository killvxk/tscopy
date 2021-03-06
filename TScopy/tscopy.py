"""
This project is based off the work from the following projects:
* https://github.com/williballenthin/python-ntfs 
* https://github.com/jschicht/RawCopy
"""
# TODO: Will have issues with non ascii characters in files names
# TODO: Currently only processes '\\.\' where RawCopy supported other formats
import logging
import sys
import os
import re
import pickle
import argparse
import time
import traceback
import struct

from math import ceil
from BinaryParser import Mmap, hex_dump, Block
from MFT import INDXException, MFTRecord, Attribute, ATTR_TYPE, Attribute_List
from MFT import StandardInformation,FilenameAttribute, INDEX_ROOT

if os.name == "nt":
    try:
        import win32file, win32api, win32con
    except:
        print "Must have pywin32 installed -- pip install pywin32"
        sys.exit(1)

####################################################################################
# BootSector structure
#   https://flatcap.org/linux-ntfs/ntfs/files/boot.html
####################################################################################
class BootSector(Block):
    def __init__(self, buf, offset, logger):
        super(BootSector, self).__init__(buf, offset)
        self.declare_field("qword", "system_id", 0x3)
        self.declare_field("word", "bytes_per_sector", 0x0b)
        self.declare_field("byte", "sectors_per_cluster", 0xd)
        self.declare_field("word", "reserved_sectors", 0xe)
        self.declare_field("byte", "media_desc", 0x15)
        self.declare_field("word", "sectors_per_track", 0x18)
        self.declare_field("word", "heads", 0x1a)
        self.declare_field("dword", "hidden_sectors", 0x1c)
        self.declare_field("qword", "total_sectors", 0x28)
        self.declare_field("qword", "start_c_mft", 0x30)
        self.declare_field("qword", "start_c_mftmir", 0x38)
        self.declare_field("byte", "file_rec_indicator", 0x40)
        self.declare_field("byte", "idx_buf_size_indicator", 0x44)
        self.declare_field("qword", "serial_number", 0x48)
        self.bytes_per_cluster = self.bytes_per_sector() * self.sectors_per_cluster()
        #COPIED FROM  RAWCOPY:: A really lame fix for a rare bug seen in certain Windows 7 x64 vm's
        if self.file_rec_indicator() > 127:
            testval = 256 - self.file_rec_indicator()
            self.mft_record_size = 2
            for i in range(testval-1):
                self.mft_record_size *= 2
        else:
            self.mft_record_size = self.bytes_per_cluster * self.file_rec_indicator()
            
        self.sectors_per_mft_record = self.mft_record_size / self.bytes_per_sector()
        self.cluster_per_file_record_segment = int(ceil(float(self.mft_record_size) / self.bytes_per_cluster))
        

####################################################################################
#  NTFS INDX Record structure
#     https://flatcap.org/linux-ntfs/ntfs/concepts/index_record.html 
####################################################################################
class INDX( Block ):
    def __init__(self, buf, offset ):
        super(INDX, self).__init__(buf, offset)
        self.declare_field("dword", "magic", 0x0)
        self.declare_field("word", "update_seq_offset", 0x4)
        self.declare_field("word", "update_seq_sz", 0x6)
        self.declare_field("qword", "logfile_seq_num", 0x8)
        self.declare_field("qword", "VCN_INDX", 0x10)
        self.declare_field("dword", "index_entries_offset", 0x18)
        self.declare_field("dword", "index_entries_sz", 0x1c)
        self.declare_field("dword", "alloc_sz", 0x20)
        self.declare_field("byte", "leaf_node", 0x24)
        self.declare_field("word", "update_seq", 0x28)
        s = self.update_seq_sz()

    def update_seq_arr( self, idx_buf ):
        # TODO: Clean this up into a for loop
        seq_arr = idx_buf[self.update_seq_offset()+2:self.update_seq_offset()+2+self.update_seq_sz()*2]
        ret  = idx_buf[0x0000:0x01fe] + seq_arr[0x00:0x2]
        ret += idx_buf[0x0200:0x03fe] + seq_arr[0x02:0x4]
        ret += idx_buf[0x0400:0x05fe] + seq_arr[0x04:0x6]
        ret += idx_buf[0x0600:0x07fe] + seq_arr[0x06:0x8]
        ret += idx_buf[0x0800:0x09fe] + seq_arr[0x08:0xa]
        ret += idx_buf[0x0a00:0x0bfe] + seq_arr[0x0a:0xc]
        ret += idx_buf[0x0c00:0x0dfe] + seq_arr[0x0c:0xe]
        ret += idx_buf[0x0e00:0x0ffe] + seq_arr[0x0e:0x10]
        ret += idx_buf[0x1000:      ] 
        return ret

####################################################################################
#  NTFS INDX Entry Structure
#     https://flatcap.org/linux-ntfs/ntfs/concepts/index_entry.html
####################################################################################
class INDX_ENTRY( Block ):
    def __init__(self, buf, offset):
        super(INDX_ENTRY, self).__init__(buf, offset)
        self.declare_field("qword", "mft_recordnum", 0)
        self.declare_field("word", "entry_sz", 0x08 )
        if self.entry_sz() == 0x18 and self.mft_recordnum() == 0:
            raise INDXException("End of INDX File found")
        if self.entry_sz() == 0x10 and self.mft_recordnum() == 0:
            raise INDXException("End of INDX File found")
        if self.entry_sz() == 0x00 and self.mft_recordnum() == 0:
            raise INDXException("NULLS INDX File found")
        self.declare_field("word", "filename_offset", 0x0a )
        self.declare_field("word", "index_flags", 0x0c )
        self.declare_field("qword", "mft_parent_recordnum", 0x10 )
        self.declare_field("qword", "alloc_sz", 0x38 )
        self.declare_field("qword", "file_sz", 0x40 )
        self.declare_field("qword", "file_flags", 0x48 )
        self.declare_field("byte", "filename_sz", 0x50 )
        self.declare_field("binary", "filename", 0x52, self.filename_sz()*2 )


####################################################################################
#  The main class of TScopy.
#     * Is a singleton instance
#     * Example usage
#       config = {'outputbasedir':dst, 'pickledir':dir,'logger':log,'debug':False,'ignore_table':False}
#       tscopy = TScopy()
#       tscopy.setConfiguration( config )
#       tscopy.copy( src, dst )
#
#     * Config key descriptions
#       - outputbasedir : The FULL PATH of directory where the files will be copied too.
#       - pickledir : The FULL PATH of directory where the pickle file will be created or used.
#       - logger : A preconfigured instance of the python Logger class. 
#       - debug : Not used
#       - ignore_table: 
#           * True  = Rebuilds the MFT table from the root node and does not save the table at the end of the run
#           * False = Uses a previous mft.pickle file if found. Saves the file after every copy.
####################################################################################
class TScopy( object ):
    _instance = None
    def __new__( cls ):
        if cls._instance == None:
            cls._instance = super(TScopy, cls).__new__(cls)
            cls.__isConfigured = False
            cls.__pickle_filename = "mft.pickle"
            cls.config = { 'files': None,
                            'pickledir': None,
                            'logger': None,
                            'debug': True,
                            'ignore_table':False,
                          }
            cls.__useWin32 = False
        return cls._instance

    ####################################################################################
    #  isConfigured:  Verifies that the object has  been configured at least once
    ####################################################################################
    def isConfigured( self ):
        return self.__isConfigured

    ####################################################################################
    # setConfiguration:  Parses the config dictionary to set the values for debug, logger,
    #                    lookuptable and the picke directory
    ####################################################################################
    def setConfiguration( self, config ):
        if self.__isConfigured == True:
            return
        self.__MFT_lookup_table = None
        self.__isConfigured = True
        self.setDebug( config['debug'] )
        self.setLogger( config['logger'] )
        self.setLookupTable( config['ignore_table'] )
        self.setPickleDir( config['pickledir'] )


    ####################################################################################
    # SetLogger:  Sets the class object logger variable
    #       Needs to be preconfigured
    ####################################################################################
    def setLogger( self, logger ):
        if logger == None:
            raise Exception( "TSCOPY", "Invalid Logger")
        self.config['logger'] = logger

    ####################################################################################
    # setDebug: Sets the class object debugger variable
    ####################################################################################
    def setDebug( self, debug ):
        self.config['debug'] = debug

    ####################################################################################
    # setLookuptable: Sets the class object ignore_table.
    ####################################################################################
    def setLookupTable( self, tf ):
        self.config['ignore_table'] = tf

    ####################################################################################
    #  setPickleDir: Sets the output directory to save the mft.pickle file too
    ####################################################################################
    def setPickleDir( self, directory ):
        if not directory == None and not os.path.isdir( directory ):
            self.config['logger'].error("Error pickle destination (%s) not found" % directory)
            parser.print_help()
            raise Exception( "TSCOPY", "Error pickle destination (%s) not found" % directory)
        self.__pickle_fullpath = '%s%s%s' % ( directory, os.sep, self.__pickle_filename )
        self.__MFT_lookup_table = self.__getLookupTableFromDisk( "c" )
        
    ####################################################################################
    #  __getLookupTableFromDisk: Checks the mft.pickle file. 
    #       If it exists then it loads into memory.
    #       If it does not exists then it creates a new basic structure
    ####################################################################################
    def __getLookupTableFromDisk( self, drive_letter ):
        if not os.path.isfile( self.__pickle_fullpath):
            return {drive_letter:{5:{'seq_num': 5, 'name':'','children':{}}}}
        try:
            self.config['logger'].debug("Using Pickle file: %s " % self.__pickle_fullpath)
            with open( self.__pickle_fullpath, 'rb') as fd:
                return pickle.loads( fd.read() )
        except:
            raise Exception( "TSCOPY", "FAILED to parse pickle file %s" % self.__pickle_fullpath )
        
    ####################################################################################
    #  __saveLookuptable: Write the lookup table from memory to disk. 
    #       Overwrites previous copy if it exists.
    ####################################################################################
    def __saveLookuptable( self, lookup_table ):
        with open(self.__pickle_fullpath, 'wb') as fd:
            fd.write( pickle.dumps( lookup_table ))

    ####################################################################################
    # __getMFT: Gets the root record of the MFT 
    ####################################################################################
    def __getMFT( self, index=0 ):
        fd = self.config['fd']
        bss = self.config['bss']
        mft_offset = bss.bytes_per_sector() * bss.sectors_per_cluster() * bss.start_c_mft()
        if self.__useWin32 == False:
            mft_offset = 0x400
#        win32file.SetFilePointer( fd, mft_offset+(index*bss.mft_record_size ), win32file.FILE_BEGIN)
#        buf = win32file.ReadFile( fd, bss.mft_record_size )[1]
        buf = self.__read( fd, mft_offset+(index*bss.mft_record_size ), bss.mft_record_size ) 
        record = MFTRecord(buf, 0, None)
        ret = {}

        attribute = record.data_attribute()
        cnt = 0
        for offset, length in attribute.runlist().runs():
            if length > 16 and (length%16) > 0:
                if offset == 0:
                     # may be sparse section at end of Compression Signature
                     ret[cnt] = (offset, length%16)
                     length -= length%16
                     cnt += 1
                else:
                     #may be compressed data section at start of Compression Signature
                     ret[cnt] = (offset, length-length%16)
                     offset += length-length%16
                     length = length%16
                     cnt += 1
            #just normal or sparse data
            ret[cnt] = (offset, length)
            cnt += 1
        
        return ret

    ####################################################################################
    #  __GenRefArray: Iterates through the seq_num 5 datadruns 
    ####################################################################################
    def __GenRefArray( self ):
        MFTClustersToKeep = 0
        ref = -1
        dataruns = self.config['mft_dataruns']
        bytes_per_cluster = self.config['bss'].bytes_per_cluster 
        ClustersPerFileRecordSegment = self.config['bss'].cluster_per_file_record_segment 
        split_mft_rec = {} 
        cnt = 0
        for x in dataruns:
            r = dataruns[x]
            doKeepCluster = MFTClustersToKeep
            MFTClustersToKeep = (r[1]+ClustersPerFileRecordSegment - MFTClustersToKeep) % ClustersPerFileRecordSegment
            if not MFTClustersToKeep == 0:
                MFTClustersToKeep = ClustersPerFileRecordSegment - MFTClustersToKeep
            pos = r[0] * bytes_per_cluster 
            subtr = self.config['bss'].mft_record_size 
            if  MFTClustersToKeep or doKeepCluster:
                subtr = 0
            end_of_run = r[1] * bytes_per_cluster - subtr
            for i in range(0, end_of_run, self.config['bss'].mft_record_size):
                if MFTClustersToKeep:
                    if i >= end_of_run - ((ClustersPerFileRecordSegment - MFTClustersToKeep) * bytes_per_cluster):
                        bytesToGet = (ClustersPerFileRecordSegment - MFTClustersToKeep) * bytes_per_cluster
                        split_mft_rec[cnt] = '%d?%d,%d' % (ref+1, pos+i, bytesToGet )
                ref += 1
                if i == 0 and doKeepCluster:
                    bytesToGet = doKeepCluster * bytes_per_cluster
                    if bytesToGet > self.config['bss'].mft_record_size:
                        bytesToGet = self.config['bss'].mft_record_size 
                    split_mft_rec[cnt] += '|%d&%d' % ( pos+i, bytesToGet )
                cnt += 1
        self.config['split_mft_rec'] = split_mft_rec

    ####################################################################################
    #  __process_image: TODO 
    ####################################################################################
    def __process_image( self, targetDrive ):
        pass

    ####################################################################################
    # __search_mft: Iterates through the target files path, populating the table and seq_path
    #           with each branch of the path as it parses the MFT records. The search ends when 
    #           it fails to find the next item in the target path or the target is identified.
    #       table: The pointer to the current location into the mft metadata table stored in memory
    #       tmp_path: The target directory path as a list
    #       seq_path: A list of the found target dirctory path with mft sequesnce numbers   
    ####################################################################################
    def __search_mft( self, table, tmp_path, seq_path ):
        for name in tmp_path:
            index = table['seq_num']
            self.config['logger'].debug('Looking for (%s) MFT_INDEX(%016X)' % (name, index))
            ret = self.__getChildIndex( index )
            self.config['logger'].debug("childindex = %r" % len(ret) )
            tmp_index = index
            for seq_num in ret:
                c_index = seq_num & 0xffffffff
                c_name = ret[seq_num].lower()
                table['children'][c_name] = { 'name':c_name, 'seq_num':c_index, 'children':{}}
                if c_name == name.lower():
                    index = c_index
                    seq_path.append( (index, c_name ) )
                    table = table['children'][c_name]
                    break
            if tmp_index == index:
#                self.config['logger'].info("%s NOT FOUND" % name)
                return None, None, None
        return table, tmp_path, seq_path
    ####################################################################################
    #  __find_last_known_path: Iterates through the target files path and matches with the 
    #           currently known indexes in the table. Returns as soon as the next path item 
    #           is not found or the end target has been located.
    #       table: The pointer to the current location into the mft metadata table stored in memory
    #       tmp_path: The target directory path as a list
    #       seq_path: A list of the found target dirctory path with mft sequesnce numbers   
    ####################################################################################

    def __find_last_known_path( self, table, tmp_path, seq_path  ):
        l_path = tmp_path[:]
        for name in l_path:
            name = name.lower()
            if not name in table['children']:
                break
            table = table['children'][name]
            tmp_path = tmp_path[1:]
            seq_path.append( ( table['seq_num'], name ))
        return table, tmp_path, seq_path

    ####################################################################################
    #  __copydir: Copies the entire directory. If bRecursive this function calls itself with 
    #           any child drictories
    #       fname: fullpath of the dirctory to copy
    #       index: Sequence number of the MFT record of the parent:
    #       table: Pointer to the current index in the MFT metadata table
    #       bRecursive:  
    #           True: When the parents child is a directory __copydir is called recursivly
    #           False: Does not copy child directories
    ####################################################################################
    def __copydir( self, fname, index, table, bRecursive=False):
        self.config['logger'].debug('fname(%r) index(%r)' % (fname, index) )
        table = self.__copydirfiles( fname, index, table )

        if bRecursive == True:
            for dirs in table['children']:
                l_table = table['children'][dirs]
                c_index = l_table['seq_num']
                buf = self.__calcOffset( c_index )
                if buf == None or len(buf) == 0:
                    raise Exception("Failed to process mft_offset")
                record = MFTRecord(buf, 0, None)
                if record.is_directory():
                    self.config['logger'].debug( "Next Directory %r  %r %r" % (c_index, dirs, fname))
                    self.config['current_file'] = fname[2:]
                    self.__copydir( os.path.join(fname,dirs), c_index, l_table, bRecursive=True )
        
    ####################################################################################
    # __copydirfiles: Wraps __getFile and copies all the files under the current directory
    #       fname: fullpath of the dirctory to copy
    #       index: Sequence number of the MFT record of the parent:
    #       table: Pointer to the current index in the MFT metadata table
    ####################################################################################
    def __copydirfiles( self, fname, index, table ):
        self.config['logger'].debug( "copydirfiles \n\tfname:\t%r\n\tindex:\t%r\n\ttable %r" % (fname,index,table))
        if table['children'] == {}:
            ret = self.__getChildIndex( index )
            self.config['logger'].debug( "\tchildren: %r" % len(ret))
            for seq_num in ret: 
                c_index = seq_num & 0xffffffff
                c_name = ret[seq_num].lower()
                table['children'][c_name] = { 'name':c_name, 'seq_num':c_index, 'children':{}}

                if ret[seq_num].strip() == '' or seq_num == 0:
                    continue

        tmp_filename = self.config['current_file']
        for name in table['children']:
            seq_num = table['children'][name]['seq_num']
            self.config['logger'].debug("\tCopying %s to %s" % (fname+os.sep+name, self.config['outputbasedir']+tmp_filename+os.sep+name))

            self.config['current_file'] = fname[2:]+os.sep+name # strip the drive letter off the front
            if '*' in fname[2:]+os.sep+name:
                self.config['current_file'] = tmp_filename+os.sep+name # strip the drive letter off the front
                
            self.__getFile( [seq_num&0xffffffff, name] )
        return table

    ####################################################################################
    #  __copyfile: Internal copy function. Used to setup and parse target filename, locate
    #           previously identified paths in the mft metadata list. and then copy the file/
    #           files/ or direcotories
    #       filename: Full path to the target file/directory or wildcarded to copy
    #       mft_filename: TODO remove
    #       bRecursive: 
    #           True:  Copy all children from this directory on
    #           False: Do not copy children
    ####################################################################################
    def __copyfile( self, filename, mft_filename=None, bRecursive=False ):
        if self.__useWin32 == True:
            self.config['logger'].debug( 'filename %r' % filename)
            if not filename[:4].lower() == '\\\\.\\':
                targetDrive = '\\\\.\\'+filename[:2]
            else:
                targetDrive = filename[:6]
            
            driveLetter = targetDrive[5]
            self.config['logger'].debug( 'Target Drive %s' % driveLetter)

            self.__process_image( targetDrive ) # TODO process this to determin correct offsets

            if self.config['ignore_table'] == True:
                self.__MFT_lookup_table = {driveLetter:{5:{'seq_num':5,'name':'','children':{}}}}
            elif not driveLetter in self.__MFT_lookup_table.keys():
                self.__MFT_lookup_table = self.__MFT_lookup_table[driveLetter] = {5:{'seq_num':5,'name':'','children':{}}}
#            self.config['logger'].debug( 'Target Drive %s' % driveLetter)
        else:
            self.__MFT_lookup_table = {"c":{5:{'seq_num':5,'name':'','children':{}}}}
            targetDrive = mft_filename
            driveLetter = "c"
            self.config['logger'].debug( 'Processing the %s MFT file' % targetDrive )

        self.config['driveLetter'] = driveLetter
        fd = self.__open( targetDrive )
        self.config['fd'] = fd
        buf = self.__read( fd, 0, 0x200 ) #        buf = win32file.ReadFile( fd, 0x200)[1]
        self.config['bss'] = BootSector( buf, 0, self.config['logger'] ) 
        self.config['mft_dataruns'] = self.__getMFT( 0)
        self.__GenRefArray()

        fname = filename 
        index = 5
        
        try:
            # Find the last known directory in the MFT_lookup_table
            seq_path = [(index,None)]
            tmp_path = fname[3:].split(os.sep)
            table = self.__MFT_lookup_table[driveLetter][5]

            expandedWildCards = self.__process_wildcards( filename, table )
            if expandedWildCards == False:
                cp_files = [ tmp_path ]
            else:
                cp_files = expandedWildCards

            
            for cp_file in cp_files:
                self.config['current_file'] = os.sep.join(cp_file) # strip the drive letter off the front
                l_fname = fname[:3] + self.config['current_file']
                self.config['logger'].info("Copying %s to %s" % (l_fname, self.config['outputbasedir']+self.config['current_file']))
                table, tmp_path, seq_path = self.__get_file_mft_seqid( cp_file )
                
                # Index was not located exit (error message already logged)
                if table == None:
                    return

                # Check the mft structure if this is a directory
                index = seq_path[-1][0]
                buf = self.__calcOffset( index )
                if buf == None or len(buf) == 0:
                    raise Exception("Failed to process mft_offset")
                record = MFTRecord(buf, 0, None)
                if record.is_directory():
                    self.__copydir( l_fname, index, table, bRecursive=bRecursive )
                else:
                    self.__getFile( seq_path[-1] )
        except:
            self.config['logger'].error(traceback.format_exc())
        finally:
            if self.config['ignore_table'] == False:
                self.__saveLookuptable( self.__MFT_lookup_table)                

    ####################################################################################
    # __isSplitMFT: Determines if the MFT record is split
    ####################################################################################
    def __isSplitMFT( self, array, target_seq_num ):
        for ind in array:
            i = array[ind]
            if not '?' in i:
                continue
            ind = i.index('?')
            testRef = i[0:ind]   
            if int(testRef) == target_seq_num:
                return ind 
        return None

    ####################################################################################
    #  __GetChildIndex: Parses the MFT records to find all children of the current sequence ID
    #       index: Sequence ID or seq_num of the current MFT record to extract and parse
    ####################################################################################
    def __getChildIndex( self, index  ):
        fd = self.config['fd']
        bss = self.config['bss']
        bpc = bss.bytes_per_cluster

        buf = self.__calcOffset( index )
        if buf == None or len(buf) == 0:
            raise Exception("Failed to process mft_offset")
        record = MFTRecord(buf, 0, None)
        if not record.is_directory():
            return []
        ret  = {}
        for attribute in record.attributes():
            if attribute.type() == ATTR_TYPE.INDEX_ROOT:
                for entry in INDEX_ROOT(attribute.value(), 0).index().entries():
                    refNum = entry.header().mft_reference() & 0xfffffffff
                    if refNum in ret:
                        if "~" in ret[refNum]:
                            ret[refNum] = entry.filename_information().filename()  
                    else:
                        ret[refNum] = entry.filename_information().filename()  
            elif attribute.type() == ATTR_TYPE.ATTRIBUTE_LIST:
                self.config['logger'].debug("ATTRIBUTE_LIST HAS BEEN FOUND 0x(%08x)!!!!" % index )
                attr_list = Attribute_List(attribute.value(), 0, attribute.value_length(), self.config['logger'] )
                self.config['logger'].debug(hex_dump(attribute.value()[:attribute.value_length()]))
                a_list = []
                for entry in attr_list.get():
                    if (entry.type() == ATTR_TYPE.INDEX_ROOT or entry.type() == ATTR_TYPE.INDEX_ALLOCATION ) and not (entry.baseFileReference()&0xffffffff) == index:
                        if not entry.baseFileReference() in a_list:
                            a_list.append( entry.baseFileReference() & 0xffffffff   )
                for next_index in a_list:
                    # WARNING!!! Recursive
                    if index == next_index:
                        self.config['logger'].debug(hex_dump(attribute.value()[:attribute.value_length()]))
#                        raise Exception("Attribute_list failed to parse.")
                        continue
                    rec_children = self. __getChildIndex( next_index )
                    self.config['logger'].debug("ATTRIBUTE_LIST index(%d) children (%r) " % (next_index, rec_children) )
                    ret.update( rec_children )
            elif attribute.type() == ATTR_TYPE.INDEX_ALLOCATION:
                for cluster_offset, length  in attribute.runlist().runs():
                    offset=cluster_offset*bpc
                    buf = self.__read( fd, offset, length*bpc)
                    for cnt in range(length):
                        idx_buf = buf[cnt*bpc:(cnt+2)*bpc]
                        ind = INDX( idx_buf, 0 )
                        idx_buf = ind.update_seq_arr( idx_buf )
                        entry_offset = ind.index_entries_offset()+0x18 
                        i = 0 
                        last_i = i
                        while i < ind.index_entries_sz() :
                            try:
                                entry  = INDX_ENTRY( idx_buf, entry_offset )
                                refNum = entry.mft_recordnum() & 0xfffffffff
                                if refNum in ret:
                                    if "~" in ret[refNum]:
                                        ret[refNum] = entry.filename().replace('\x00','')
                                else:
                                    ret[refNum] = entry.filename().replace('\x00','')
                            except   INDXException:
                                break
                            except:
                                self.config['logger'].error(traceback.format_exc())
                                self.config['logger'].debug( 'len(idx_buf (%03x) entry_offset(%03x)' % ( len(idx_buf), entry_offset))
                                pass
                            entry_offset += entry.entry_sz()

                            i += entry.entry_sz()
                            if entry.entry_sz() == 0:
                                break
        return ret

    ####################################################################################
    # __calcOffset: Calculates the offset into the drive to locat the specific data 
    #       for the taget sequence Number
    #   target_seq_num: Sequence ID to copy form the disk
    ####################################################################################
    def __calcOffset( self, target_seq_num ):
        fd = self.config['fd']
        bss = self.config['bss']
        mft_vcn = self.config['mft_dataruns']
        image_offset = 0 # TODO: Change this when finished processing the image
        array = self.config['split_mft_rec']

        # Handle in the case that the object is split accross two dataruns
        split = self.__isSplitMFT( array, target_seq_num )
        if not split == None:
#            self.config['logger'].debug( 'calcOffset: a split record was detected' )
            item = array[split]
            ind = item.index('?')
            testRef = item[0:ind]   
            if not int(testRef) == target_seq_num:
#                self.config['logger'].debug("Error: The ref in the array did not match target ref.")
                return None
            
            srecord3 = item[ind+1:]
            srecordArr = srecord3.split('|')
            if not len( srecordArr ) == 3:
#                self.config['logger'].debug("Error: Array contained more elements than expected: %d" % len( srecordArr ))
                return None

            record = ""
            for i in srecordArr:
                if not ',' in i: 
#                    self.config['logger'].debug('Split:: Could not find ","')
                    continue
                ind = i.index(',')
                srOffset = i[:ind]
                srSize   = i[ind+1:]
#                win32file.SetFilePointer( fd, srOffset + image_offset, win32file.FILE_BEGIN)
#                record += win32file.ReadFile( fd, srSize)[1]
                record += self.__read( fd, srOffset + image_offset, srSize )
            return record
        else:
            counter = 0
            offset = 0
            recordsdivisor = bss.mft_record_size/512
            for indx in mft_vcn: 
                current_cluster = mft_vcn[indx][1]
                offset = mft_vcn[indx][0]
                records_in_currentrun = (current_cluster * bss.sectors_per_cluster() ) / recordsdivisor 
                counter += records_in_currentrun 
                if counter > target_seq_num:
                    break
            tryat = counter - records_in_currentrun
            records_per_cluster = bss.sectors_per_cluster() / recordsdivisor
            final = 0
            counter2 = 0
            record_jmp = 0
            while final < target_seq_num:
                record_jmp += records_per_cluster
                counter2 += 1
                final = tryat + record_jmp
            records_to_much = final - target_seq_num

            mft_offset = image_offset + offset * bss.bytes_per_cluster + ( counter2 * bss.bytes_per_cluster ) - ( records_to_much * bss.mft_record_size )
#            win32file.SetFilePointer( fd, mft_offset, win32file.FILE_BEGIN)
#            return win32file.ReadFile( fd, bss.mft_record_size )[1]
            if self.__useWin32 == False:
                mft_offset = 0x400 + 0x400*target_seq_num
#            self.config['logger'].debug('Split:: mft_offset(%r) record_size(%r)' % ( mft_offset, bss.mft_record_size))
            return self.__read( fd, mft_offset, bss.mft_record_size )
        return None

    ####################################################################################
    # __getFile: The required file was identified this function locates all the parts of 
    #           the file and writes them in order to the destination location
    #       mft_file_object:
    ####################################################################################
    def __getFile( self, mft_file_object ):
        if self.__useWin32 == False:
            return

        fd = self.config['fd']
        bpc = self.config['bss'].bytes_per_cluster

        buf = self.__calcOffset( mft_file_object[0] )

        if buf == None:
            raise Exception("Failed to process mft_offset")
        try:
            record = MFTRecord(buf, 0, None)
            for attribute in record.attributes():
                if attribute.type() == ATTR_TYPE.DATA:
                    fullpath = self.config['outputbasedir'] + self.config['current_file']
#                    self.config['logger'].debug( "GetFile:: fullpath %s" % fullpath )
#                    self.config['logger'].debug( "GetFile:: attributes %s" % attribute.get_all_string())
                    path = '\\'.join( fullpath.split('\\')[:-1])
                    if not os.path.isdir( path ): 
                        os.makedirs( path )
                    fd2 = open( fullpath,'wb' )
                    
                    try:
#                        self.config['logger'].debug("non_resident %r" % attribute.non_resident() ) 
                        if attribute.non_resident() == 0:
                            fd2.write( attribute.value()) 
                        else:
                            cnt = 0
                            padd = False
                            for cluster_offset, length in attribute.runlist().runs():
#                                self.config['logger'].debug("GetFile:: cluster_offset( %08x ) lenght( %08x )  " % ( cluster_offset, length))
                                read_sz = length * bpc 
#                                self.config['logger'].debug("readsize %08x cnt %08x init_sz %08x" % ( read_sz, cnt, attribute.initialized_size()))
                                if read_sz + cnt > attribute.initialized_size():
                                    read_sz = attribute.initialized_size() - cnt
                                    padd = True
                                if (read_sz % 0x1000) > 0:
                                    read_sz += 0x1000 - (read_sz%0x1000)
                                offset=cluster_offset * bpc

#                                self.config['logger'].debug("readsize %08x cnt %08x init_sz %08x" % ( read_sz, cnt, attribute.initialized_size()))
                                buf = self.__read( fd, offset, read_sz )

                                if attribute.data_size() < cnt + read_sz:
                                    read_sz = attribute.data_size()-cnt
                                cnt += read_sz
                                        
                                fd2.write(buf[:read_sz])
                                if padd == True:
                                    padd_sz  = attribute.data_size() - attribute.initialized_size() 
                                    fd2.write( '\x00' * padd_sz )
                                    cnt += padd_sz
                                if cnt > attribute.initialized_size():
#                                    self.config['logger'].debug("readsize %08x cnt %08x init_sz %08x" % ( read_sz, cnt, attribute.initialized_size()))
                                    break
                    except:
#                        self.config['logger'].error('Failed to get file %s' % (mft_file_object[1] ) )
                        self.config['logger'].error('Failed to get file %s\n%s' % (mft_file_object[1], traceback.format_exc() ))
                    finally:
                        fd2.close()
        except:
            self.config['logger'].error('Failed to get file %s\n%s' % (mft_file_object[1], traceback.format_exc() ))

    ####################################################################################
    # __open: Wrapper around win32file createfile. 
    #       TODO remove test code.
    ####################################################################################
    def __open( self, filename ):
        fd = None
        try:
            if self.__useWin32 == False:
                fd = open(filename, 'rb') 
            else:
                fd = win32file.CreateFile( filename,
                                win32file.GENERIC_READ,
                                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                                None, 
                                win32con.OPEN_EXISTING, 
                                win32file.FILE_ATTRIBUTE_NORMAL,
                                None)
        except:
            self.config['logger'].error( traceback.format_exc())
        return fd

    ####################################################################################
    # __read: Wrapper around win32file set file pointer and read contents. 
    #       TODO remove test code.
    ####################################################################################
    def __read( self, fd, offset, read_sz ):
        buf = ""
        try:
            if self.__useWin32 == False:
                fd.seek( offset, 0)
                buf = fd.read( read_sz )
            else:
                win32file.SetFilePointer( fd, offset, win32file.FILE_BEGIN)
                buf = win32file.ReadFile( fd, read_sz)[1]
        except:
            self.config['logger'].error( traceback.format_exc())
            self.config['logger'].debug("offset(%08x), readsize (%08x) fd (%08x)" % ( offset, read_sz, fd))
        return buf
        
    ####################################################################################
    # __get_wildcard_children:  Get the children of the wildcarded directory location
    #       path: is a tuple containing the base path and the wildcard
    # TODO Move this someplace else in the file
    ####################################################################################
    def __get_wildcard_children( self, path ):
        copy_list = []
        table, x, seq_path = self.__get_file_mft_seqid( path[0] )
        if seq_path == None:
            return copy_list
        # Test if the last value seq_path[-1] is the directory we are looking for
        if path[1] == None:
            if seq_path[-1][1] == path[0][-1]:
                copy_list.append( path[0] )

        # get children of found path and find all that match wildcard.
        ret = self.__getChildIndex( seq_path[-1][0] )
        for x in ret:
            if path[1] == None:
                    break
            l_name = ret[x].lower()
            l_reg = re.escape(path[1]).replace('\\*', '.*')
            if not l_reg[-1] == '*':
                l_reg += '$'
            if re.match( l_reg, l_name ):
                l_name =  path[0] + [ l_name ] 
                copy_list.append( l_name )
        return copy_list

    ####################################################################################
    # __get_file_mft_seqid: Wrapper used to search for the file in the current memory mft 
    #           metadata list then process the rest of the path from parsing the MFT
    #       tmp_path: List of the source path
    ####################################################################################
    def __get_file_mft_seqid( self, tmp_path ):
        index = 5
        seq_path = [(index,None)]
        table = self.__MFT_lookup_table[self.config['driveLetter']][index]
        table, tmp_path, seq_path = self.__find_last_known_path( table, tmp_path, seq_path  )
        table, tmp_path, seq_path = self.__search_mft( table, tmp_path, seq_path )
        return table, tmp_path, seq_path

    ####################################################################################
    # __process_wildcards: Called when a wildcard was detected in the source filename.
    #           Parses the wildcards and breaks up into sections then the paths are expanded
    #           and each matching record is copied.
    #       filename: Filename containing the wildcards
    #       table: Pointer to the root of the mft Metadata table
    ####################################################################################
    def  __process_wildcards( self, filename, table ):
        filename = filename.lower()
        if not '*' in filename:
            return False
        if filename[1:3] == ":\\":
            filename = filename[3:]
        
        index = 5
        seq_path = [(index,None)]
        tmp_path = filename.split( os.sep )
        path = []
        path_start = 0
        for ind in range( len(tmp_path)):
            if "*" in tmp_path[ind]:
                path.append( ( tmp_path[ path_start : ind ], tmp_path[ind]) )
                path_start = ind + 1
        if path_start < len(tmp_path):
            path.append( ( tmp_path[ path_start : ], None) )

        tList = []
        for iPath in path:
            tList = self.__regexsearch( iPath, tList ) 
        return tList

    ####################################################################################
    # __regexsearch: Searches the path to determine if it matches the wildcard. Only the
    #           '*' wildcard is supported. 
    #       path:
    #       tList:
    ####################################################################################
    def __regexsearch( self, path, tList ):
        if tList == []:
            findPaths = [ path ]
        else:
            findPaths = []
            for ePath in tList:
                findPaths.append( ( ePath + path[0], path[1] ))
        ret = []
        for fp in findPaths:
            found =  self.__get_wildcard_children( fp )
            ret.extend( found )
        return ret

            
    
    ####################################################################################
    # Copy file from a single source file or directory. Wildcards (*) are acceptable
    #   src_filename: Can be a filename, directory, or a wildcard
    #   dest_filename: The root directory to save files too. Each will create a mirror path
    #                  Example: dest_filename = 'c:\test\' and copying "c:\windows\somefile" 
    #                           the output file will have the path of "c:\test\windows\somefile"
    #   bRecursive: Tells the copy to recursivly copy a directory. Only works with directories
    ####################################################################################
    def copy( self, src_filename, dest_filename, bRecursive=False ):
        self.__useWin32 = True
        if not (dest_filename[-1] == '/' or dest_filename[-1] == '\\'):
            dest_filename = dest_filename+os.sep
        self.config['outputbasedir'] = dest_filename 
        if type(src_filename) == unicode:
            src_filename = src_filename.encode('ascii', 'ignore')
        if not type( src_filename ) == str:
            self.config['logger'].error("INVALID src type (%r)" % (src_filename ) )
            return
        src_filename = os.path.abspath( src_filename )
        src_filename = [ src_filename ]
        for filename in src_filename: 
            self.__copyfile( filename, bRecursive=bRecursive )






