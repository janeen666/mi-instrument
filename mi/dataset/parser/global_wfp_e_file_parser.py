#!/usr/bin/env python

"""
@package mi.dataset.parser.global_wfp_e_file_parser
@file marine-integrations/mi/dataset/parser/global_wfp_e_file_parser.py
@author Joe Padula
@brief A common parser for the Global E file type of the wire following profiler
Release notes:

Initial Release
"""

__author__ = 'Joe Padula'
__license__ = 'Apache 2.0'

import copy
# noinspection PyUnresolvedReferences
import ntplib
import struct
import binascii
import time

from mi.core.log import get_logger
log = get_logger()

from mi.core.exceptions import SampleException, UnexpectedDataException

from mi.dataset.parser.WFP_E_file_common import WfpEFileParser, HEADER_BYTES, STATUS_BYTES_AUGMENTED, \
    STATUS_BYTES, STATUS_START_MATCHER, WFP_E_GLOBAL_RECOVERED_ENG_DATA_SAMPLE_MATCHER, \
    WFP_E_GLOBAL_FLAGS_HEADER_MATCHER, WFP_E_GLOBAL_RECOVERED_ENG_DATA_SAMPLE_BYTES


class GlobalWfpEFileParser(WfpEFileParser):
    """
    Class used to parse the wfp e global data stream. This class is a common class that can be used
    for parsing recovered Global WFP E-type file format data streams. There are several of this type,
    including dosta_ln_wfp and flord_ln_wfp. When this class is referenced by the driver,
    a unique parser does not have to be written, just the unique particle class.
    """

    def __init__(self,
                 config,
                 state,
                 stream_handle,
                 state_callback,
                 publish_callback,
                 *args, **kwargs):
        self._saved_header = None
        super(GlobalWfpEFileParser, self).__init__(config,
                                                   state,
                                                   stream_handle,
                                                   state_callback,
                                                   publish_callback,
                                                   *args, **kwargs)

    def _parse_header(self):
        """
        This method ensures the header data matches the wfp e global flags
        """
        # read the first bytes from the file
        header = self._stream_handle.read(HEADER_BYTES)
        match = WFP_E_GLOBAL_FLAGS_HEADER_MATCHER.match(header)
        if not match:
            raise SampleException("File header does not match the header regex")

        self._saved_header = header

        # update the state to show we have read the header
        self._increment_state(HEADER_BYTES)

    def get_block(self):
        """
        This function overwrites the get_block function in dataset_parser.py
        to  read the entire file rather than break it into chunks.
        Returns:
          The length of data retrieved.
        An EOFError is raised when the end of the file is reached.
        """
        # Read in data in blocks so as to not tie up the CPU.
        block_size = 1024
        eof = False
        data = ''
        while not eof:
            next_block = self._stream_handle.read(block_size)
            if next_block:
                data = data + next_block
            else:
                eof = True

        if data != '':
            self._chunker.add_chunk(data, ntplib.system_to_ntp_time(time.time()))
            self.file_complete = True
            return len(data)
        else:  # EOF
            self.file_complete = True
            raise EOFError

    def sieve_function(self, raw_data):
        """
        This method sorts through the raw data to identify new blocks of data that need
        processing.  This is needed instead of a regex because blocks are identified by
        position in this binary file.
        """
        form_list = []
        raw_data_len = len(raw_data)

        # Starting from the end of the buffer and working backwards
        parse_end_point = raw_data_len

        # We are going to go through the file data in reverse order since we have a
        # variable length status indicator field.
        # While we do not hit the beginning of the file contents, continue
        while parse_end_point > 0:

            # Create the different start indices for the three different scenarios
            raw_data_start_index_augmented = parse_end_point-STATUS_BYTES_AUGMENTED
            raw_data_start_index_normal = parse_end_point-STATUS_BYTES
            global_recovered_eng_rec_index = parse_end_point-WFP_E_GLOBAL_RECOVERED_ENG_DATA_SAMPLE_BYTES

            # Check for an an augmented status first
            if raw_data_start_index_augmented >= 0 and \
                    STATUS_START_MATCHER.match(raw_data[raw_data_start_index_augmented:parse_end_point]):
                log.trace("Found OffloadProfileData with decimation factor")
                parse_end_point = raw_data_start_index_augmented

            # Check for a normal status
            elif raw_data_start_index_normal >= 0 and \
                    STATUS_START_MATCHER.match(raw_data[raw_data_start_index_normal:parse_end_point]):
                log.trace("Found OffloadProfileData without decimation factor")
                parse_end_point = raw_data_start_index_normal

            # If neither, we are dealing with a global wfp_sio e recovered engineering data record,
            # so we will save the start and end points
            elif global_recovered_eng_rec_index >= 0:
                log.trace("Found OffloadEngineeringData")
                form_list.append((global_recovered_eng_rec_index, parse_end_point))
                parse_end_point = global_recovered_eng_rec_index

            # We must not have a good file, log some debug info for now
            else:
                log.debug("raw_data_start_index_augmented %d", raw_data_start_index_augmented)
                log.debug("raw_data_start_index_normal %d", raw_data_start_index_normal)
                log.debug("global_recovered_eng_rec_index %d", global_recovered_eng_rec_index)
                log.debug("bad file or bad position?")
                raise SampleException("File size is invalid or improper positioning")

        return_list = form_list[::-1]
        return return_list

    def parse_chunks(self):
        """
        This method parses out any pending data chunks in the chunker. If
        it is a valid data piece, build a particle, update the position and
        timestamp. Go until the chunker has no more valid data.
        @retval a list of tuples with sample particles encountered in this
            parsing, plus the state. An empty list of nothing was parsed.
        """
        result_particles = []
        (nd_timestamp, non_data, non_start, non_end) = self._chunker.get_next_non_data_with_index(clean=False)
        (timestamp, chunk, start, end) = self._chunker.get_next_data_with_index(clean=True)
        self.handle_non_data(non_data, non_end, start)

        while chunk is not None:

            data_match = WFP_E_GLOBAL_RECOVERED_ENG_DATA_SAMPLE_MATCHER.match(chunk)
            if data_match:

                # Let's first get the 32-bit unsigned int timestamp which should be in the first match group
                fields_prof = struct.unpack_from('>I', data_match.group(1))
                timestamp = fields_prof[0]
                ntp_time = float(ntplib.system_to_ntp_time(timestamp))

                # particle-ize the data block received, return the record
                sample = self._extract_sample(self._particle_class,
                                              None,
                                              chunk,
                                              internal_timestamp=ntp_time)
                if sample:
                    # create particle
                    log.trace("Extracting sample chunk 0x%s with read_state: %s", binascii.b2a_hex(chunk),
                              self._read_state)
                    self._increment_state(len(chunk))
                    result_particles.append((sample, copy.copy(self._read_state)))

            (nd_timestamp, non_data, non_start, non_end) = self._chunker.get_next_non_data_with_index(clean=False)
            (timestamp, chunk, start, end) = self._chunker.get_next_data_with_index(clean=True)
            self.handle_non_data(non_data, non_end, start)
        return result_particles

    def handle_non_data(self, non_data, non_end, start):
        """
        This method handles any non-data that is found in the file
        """
        # if non-data is expected, handle it here, otherwise it is an error
        if non_data is not None and non_end <= start:
            # if this non-data is an error, send an UnexpectedDataException and increment the state
            self._increment_state(len(non_data))
            # if non-data is a fatal error, directly call the exception, if it is not use the _exception_callback
            self._exception_callback(UnexpectedDataException("Found %d bytes of un-expected non-data 0x%s" %
                                                             (len(non_data), binascii.b2a_hex(non_data))))
