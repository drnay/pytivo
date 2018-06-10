import logging
import os
import subprocess
import time
import struct
import sys
from functools import reduce
from threading import Thread
from datetime import datetime
from urllib.parse import urlsplit, unquote, parse_qs
from xml.dom import minidom

from tzlocal import get_localzone
import config
from metadata import prefix_bin_qty
from showinfo import DataSources


logger = logging.getLogger('pyTivo.TivoDownload')

class TivoDownload(Thread):
    """Download thread for a specific TiVo.

    TivoDownload is a Thread object which downloads all the recordings
    specified in a given tivo's queue.

    Attributes:
        tivoIP (str): The IPv4 address of the TiVo which this thread is downloading from.
        active_tivos (dict<tivoIP>): Dictionary of all TiVos with active (or pending) downloads.
        active_tivos_lock (RLock): Lock which must be acquired before accessing the
            `active_tivos` dictionary.
        tivo_tasks: The entry in `active_tivos` for this TiVo download thread (ie tivoIP)
        tivo_open (Callable[[str], http.client.HTTPResponse]): function to use to "open"
            a url to this TiVo, already initialized w/ authentication info
        decoder_path (str): path to the decoder executable (may be either
            tivodecode or tivolibre)
        decoder_is_tivolibre (bool): determines if the decoder_path is to
            tivolibre (true) or tivodecode (false)
        has_decoder (bool): true if a decoder utility was found (and its path is in `decoder_path`)

    """

    def __init__(self, tivoIP, active_tivos, active_tivos_lock, tivo_open):
        """
        Initialize the TivoDownload with the IP address of the tivo in the
        active tivo list whose queue is to be processed and a function
        to open a tivo download url.
        """
        super().__init__()
        self.tivoIP = tivoIP
        self.active_tivos = active_tivos
        self.active_tivos_lock = active_tivos_lock
        self.tivo_open = tivo_open
        with self.active_tivos_lock:
            self.tivo_tasks = self.active_tivos[tivoIP]

        # prefer tivolibre to tivodecode
        self.decoder_path = config.get_bin('tivolibre')
        self.decoder_is_tivolibre = True
        if not self.decoder_path:
            self.decoder_path = config.get_bin('tivodecode')
            self.decoder_is_tivolibre = False
        self.has_decoder = bool(self.decoder_path)


    def run(self):
        """
        The thread entrypoint. Downloads everything in the tivo_tasks
        queue, until empty, and then removes the tivo_tasks from
        the active_tivos and exits.
        """
        _prevent_computer_from_sleeping(True)

        logger.debug('start(%s) entered.', self.tivoIP)

        while True:
            self.tivo_tasks['lock'].acquire()
            if self.tivo_tasks['queue']:
                self.tivo_tasks['lock'].release()
            else:
                logger.debug('start: queue is empty for %s', self.tivoIP)
                # Complicated but... before we delete the tivo from the
                # list of active tivos we need to release the tasks lock
                # in case someone else is waiting to add an entry and
                # then we can acquire the active tivo lock and the tasks
                # lock in the correct order (so we don't deadlock), double
                # check than no tasks were added while we didn't have the
                # lock, and only then delete the tivo from the active list.
                self.tivo_tasks['lock'].release()
                with self.active_tivos_lock:
                    with self.tivo_tasks['lock']:
                        if not self.tivo_tasks['queue']:
                            del self.active_tivos[self.tivoIP]
                    break

            self.get_1st_queued_file()
            with self.tivo_tasks['lock']:
                logger.debug('start: %s removing 1st queue entry of %d', self.tivoIP, len(self.tivo_tasks['queue']))
                self.tivo_tasks['queue'].pop(0)

        with self.active_tivos_lock:
            if not self.active_tivos:
                _prevent_computer_from_sleeping(False)


    def get_1st_queued_file(self):
        """
        Download the first entry in the tivo tasks queue
        """
        tivo_name = self.tivo_tasks['tivo_name']
        mak = self.tivo_tasks['mak']
        ts_error_mode = self.tivo_tasks['ts_error_mode']
        ts_max_retries = self.tivo_tasks['ts_max_retries']

        lock = self.tivo_tasks['lock']
        with lock:
            status = self.tivo_tasks['queue'][0]
            ts_format = status['ts_format']
            url = status['url']
            dnld_url = url + ('&Format=video/x-tivo-mpeg-ts' if ts_format else '')
            decode = status['decode'] and self.has_decoder
            save_txt = status['save']
            status.update({'running': True, 'queued': False})
            showinfo = status['showinfo']
            self.get_show_details(showinfo)
            outfile = self.get_out_file(status)
            status['outfile'] = outfile

        split_dnld_url = urlsplit(dnld_url)
        dnld_qs = parse_qs(split_dnld_url.query)

        # Save the metadata file 1st unless it's already there
        # It may not exactly match the final output file name (if that
        # name is adjusted to show errors).
        # Even if the download is aborted because of errors, the metadata
        # file should remain.
        if save_txt:
            save_fn = outfile + '.txt'
            if not os.path.isfile(save_fn):
                with open(save_fn, 'w') as txt_f:
                    showinfo.write_text(txt_f)
                    logger.debug('Metadata TXT file saved: %s', save_fn)

        try:
            tivo_f_in = self.tivo_open(dnld_url)
        except ConnectionResetError as e:
            with lock:
                status['running'] = False
                status['error'] = str(e)
            return
        except Exception as e:                  # pylint: disable=broad-except
            logger.error('get_1st_queued_file: tivo_open(%s) raised %s: %s', dnld_url, e.__class__.__name__, e)
            with lock:
                status['running'] = False
                status['error'] = str(e)
            return

        if decode:
            tcmd = [self.decoder_path, '-m', mak, '-o', outfile]
            if not self.decoder_is_tivolibre:
                tcmd += '-'

            tivodecode = subprocess.Popen(tcmd, stdin=subprocess.PIPE,
                                          bufsize=(512 * 1024))
            f = tivodecode.stdin
        else:
            f = open(outfile, 'wb')

        start_time = time.time()
        download_aborted = False
        retry_download = False
        sync_loss = False
        tivo_header_size = 0

        logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Start getting "{fname}" from {tivo_name}'
                    .format(timestamp=datetime.fromtimestamp(start_time),
                            fname=outfile, tivo_name=tivo_name))

        with tivo_f_in, f:
            try:
                # Download just the header first so remaining bytes are packet aligned for TS
                output = self.get_tivo_header(tivo_f_in)
                f.write(output)
                tivo_header_size = len(output)
                with lock:
                    status['size'] = tivo_header_size

                # Download the rest of the tivo file
                download_aborted, retry_download = self.copy_tivo_body_to(tivo_f_in, f)

                with lock:
                    # temporarily put back variables removed by refactored loop
                    sync_loss = bool(status['ts_error_packets'])

                    # TODO: figure out why this code
                    if status['running']:
                        if not sync_loss:
                            status['error'] = ''

            except Exception as e:              # pylint: disable=broad-except
                download_aborted = True
                with lock:
                    status['error'] = 'Error downloading file: {}'.format(e)
                    status['running'] = False
                    bytes_read = status['size']
                    # If we've got retries left (even though this is aborting
                    # due to an exception) let's try again
                    if status['retry'] < ts_max_retries:
                        retry_download = True

                logger.error('ToGo.get_1st_queued_file(%s, id=%s) raised %s: %s\n\tr:%s; retry: %s',
                             self.tivoIP, dnld_qs['id'], e.__class__.__name__, e, format(bytes_read, ',d'),
                             'yes' if retry_download else 'no')

        end_time = time.time()
        elapsed = (end_time - start_time) if end_time >= start_time + 1 else 1
        with lock:
            bytes_read = status['size']
            rate = (bytes_read * 8.0) / elapsed
            status['rate'] = rate

        # if we were decoding wait for the decode subprocess to exit
        if decode:
            while tivodecode.poll() is None:
                time.sleep(1)

        # Fill in some of this attempt's information
        # attempt_statuses = ('unknown', 'succeeded', 'aborted', 'sync_errors_saved', 'sync_errors_aborted')
        with lock:
            download_attempt = {'status': 'unknown',
                                'start_time': start_time,
                                'size': status['size'],
                                'download_time': elapsed,
                                'error_packet_count': reduce(lambda total, x: total + x[1], status['ts_error_packets'], 0)
                               }
            if sync_loss:
                download_attempt['error_packets'] = [{'count': lost[1],
                                                      'start': int(tivo_header_size + lost[0] * TS_PACKET_SIZE),
                                                      'end':   int(tivo_header_size + (lost[0] + lost[1]) * TS_PACKET_SIZE)}
                                                     for lost in status['ts_error_packets']]

        # if we read and wrote the entire download file
        if not download_aborted:
            download_attempt['status'] = 'succeeded'
            ts_error_count = download_attempt['error_packet_count']

            with lock:
                status['running'] = False
                best_file = status['best_file']

            logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Done getting "{fname}" from {tivo_name}, '
                        '{mbps[0]:.2f} {mbps[1]}b/s ({num_bytes[0]:.3f} {num_bytes[1]}Bytes / {seconds:.0f} s)'
                        .format(timestamp=datetime.fromtimestamp(end_time),
                                fname=outfile, tivo_name=tivo_name,
                                num_bytes=prefix_bin_qty(bytes_read),
                                mbps=prefix_bin_qty(rate),
                                seconds=elapsed))

            # We're here if there were no sync errors, or we're saving all attempts or
            # this last attempt has fewer sync errors than the previous best attempt
            if ts_error_mode == 'best' and os.path.isfile(best_file):
                os.remove(best_file)

            if sync_loss:
                download_attempt['status'] = 'sync_errors_saved'

                outfile_name = outfile.split('.')
                # Add errors(lost packet count) and attempt number to the output file name
                outfile_name[-1:-1] = [' (^{}_{})'.format(ts_error_count, status['retry'] + 1), '.']
                new_outfile = ''.join(outfile_name)

                # if the new filename exists, append a count until an unused name is found
                if os.path.isfile(new_outfile):
                    count = 2
                    outfile_name[-2:-2] = [' ({})'.format(count)]

                    while os.path.isfile(new_outfile):
                        count += 1
                        outfile_name[-3] = ' ({})'.format(count)
                        new_outfile = ''.join(outfile_name)

                os.rename(outfile, new_outfile)
                outfile = new_outfile

            with lock:
                if not status['best_file'] or ts_error_count < status['best_error_count']:
                    status['best_file'] = outfile
                    status['best_error_count'] = ts_error_count
                    status['best_attempt_index'] = len(status['download_attempts'])
                status['download_attempts'].append(download_attempt)

        else:
            # aborted download
            download_attempt['status'] = 'sync_errors_aborted' if sync_loss else 'aborted'

            os.remove(outfile)
            with lock:
                status['download_attempts'].append(download_attempt)
                logger.info('[%s] Aborted transfer (%s) of "%s" from %s',
                            time.strftime('%d/%b/%Y %H:%M:%S'), status['error'], outfile, tivo_name)

        if not retry_download:
            with lock:
                status['finished'] = True
                best_attempt = status['download_attempts'][status['best_attempt_index']]
                best_error_count = best_attempt['error_packet_count']

            self.write_syncerr_log()

            if best_error_count > 0:
                best_error_packets = best_attempt['error_packets']
                logger.info('[{timestamp:%d/%b/%Y %H:%M:%S}] Done (with errors: '
                            '{epackets} packets in {esections} pieces (largest: {elargest}); '
                            '{ebytes[0]:.3f} {ebytes[1]}Bytes total)'
                            .format(timestamp=datetime.fromtimestamp(end_time),
                                    epackets=best_error_count,
                                    esections=len(best_error_packets),
                                    ebytes=prefix_bin_qty(best_error_count * TS_PACKET_SIZE),
                                    elargest=reduce(lambda largest, x: largest if largest > x['count'] else x['count'],
                                                    best_error_packets, 0)))
        else:
            logger.debug('get_1st_queued_file: retrying download, adding back to the queue')
            with lock:
                retry_status = status.copy()
            retry_status.update({'rate': 0,
                                 'size': 0,
                                 'queued': True,
                                 'retry': retry_status['retry'] + 1,
                                 'ts_error_packets': []})

            logger.info('Transfer error detected, retrying download (%d/%d)',
                        retry_status['retry'], ts_max_retries)
            with lock:
                self.tivo_tasks['queue'][1:1] = [retry_status]


    @staticmethod
    def get_tivo_header(f):
        """
        Get the tivo header from f, leaving f positioned after the header.
        """
        tivo_header = bytearray(f.read(16))
        tivo_header_size = struct.unpack_from('>L', tivo_header, 10)[0]
        tivo_header += f.read(tivo_header_size - 16)
        return tivo_header


    def copy_tivo_body_to(self, in_f, out_f):
        """
        Copy the body of the tivo file from in_f to out_f.

        in_f must be positioned after the tivo header.
        tivo_header_size is used only for logging.
        """
        ts_error_mode = self.tivo_tasks['ts_error_mode']
        ts_max_retries = self.tivo_tasks['ts_max_retries']

        lock = self.tivo_tasks['lock']
        with lock:
            status = self.tivo_tasks['queue'][0]
            ts_format = status['ts_format']
            tivo_header_size = status['size']

        bytes_read = 0              # bytes read from download http connection
        bytes_written = 0           # bytes written to file or tivo decoder
        start_time = time.time()
        download_aborted = False
        retry_download = False

        # set the starting interval values (that are reset when the
        # status rate and size are updated)
        last_interval_start = start_time
        last_interval_read = bytes_read

        # Read a chunk of the file at a time. It must be a multiple of the
        # TS packet size of 188 for the TS sync checking in the loop to work.
        chunk_size = 524144

        # Download the body of the tivo file
        while True:
            output = in_f.read(chunk_size) # Size needs to be divisible by 188
            bytes_read += len(output)
            last_interval_read += len(output)

            if not output:
                break

            if ts_format:
                buf_packets_lost = packets_with_sync_loss(output)

                if buf_packets_lost:
                    output_start_packet = bytes_read / TS_PACKET_SIZE
                    new_packets_lost = [(x[0] + output_start_packet, x[1]) for x in buf_packets_lost]

                    for lost in new_packets_lost:
                        logger.info('TS sync loss detected: %d packets (%d bytes) at offset [%d - %d)',
                                    lost[1],
                                    lost[1] * TS_PACKET_SIZE,
                                    tivo_header_size + lost[0] * TS_PACKET_SIZE,
                                    tivo_header_size + (lost[0] + lost[1]) * TS_PACKET_SIZE)
                    with lock:
                        status['ts_error_packets'] += new_packets_lost
                        ts_error_count = reduce(lambda total, x: total + x[1], status['ts_error_packets'], 0)

                        if ts_error_mode != 'ignore':
                            # we found errors and we don't want to ignore them so
                            # if we have retries left schedule a retry
                            if status['retry'] < ts_max_retries:
                                retry_download = True

                            # if we are keeping the best download of all attempts
                            # and we've already got more errors than a previous try
                            # abort this download and move on to the next attempt
                            if ts_error_mode == 'best':
                                if status['retry'] > 0 and status['best_file']:
                                    if ts_error_count >= status['best_error_count']:
                                        status['running'] = False
                                        status['error'] = ('TS sync error. Best({}) < Current({})'
                                                           .format(status['best_error_count'], ts_error_count))
                                        download_aborted = True
                                        break

                            # if we don't want to keep a download with any errors
                            # abort now (we'll try again if there were tries left)
                            elif ts_error_mode == 'reject':
                                status['running'] = False
                                status['error'] = 'TS sync error. Mode: reject'
                                download_aborted = True
                                break

            out_f.write(output)
            bytes_written += len(output)

            # Update the amount downloaded and download speed (so it can be accessed
            # and reported from a different thread.
            now = time.time()
            elapsed = now - last_interval_start
            if elapsed >= 1:
                with lock:
                    status['rate'] = (last_interval_read * 8.0) / elapsed
                    status['size'] += last_interval_read
                last_interval_read = 0
                last_interval_start = now

        return download_aborted, retry_download


    def get_show_details(self, showinfo):
        """
        Get more metadata about the show.
        """
        # Don't bother if we've already gotten the tivo details
        if DataSources.TIVO_ITEM_DETAILS in showinfo.data_sources:
            return

        try:
            with self.tivo_open(showinfo['details_url']) as details_f_in:
                showinfo.from_tivo_details(minidom.parse(details_f_in))
        except Exception as e:                  # pylint: disable=broad-except
            logger.error('get_show_details: raised %s: %s', e.__class__.__name__, e)
            return

    def write_syncerr_log(self):
        """
        Write out the sync error log of the current(complete) download in YAML
        """
        lock = self.tivo_tasks['lock']
        with lock:
            status = self.tivo_tasks['queue'][0]
            tivo_name = self.tivo_tasks['tivo_name']
            outfile = status['outfile']
            download_attempts = status['download_attempts']
            best_ndx = status['best_attempt_index']
            best_attempt = download_attempts[best_ndx]

        best_size = best_attempt['size']
        best_start_time = best_attempt['start_time']
        best_error_packet_count = best_attempt['error_packet_count']

        # replace the outfile extension w/ the syncerr log extension
        outfile_parts = outfile.split('.')
        outfile_parts[-1] = 'syncerr.yaml'
        syncerr_fn = '.'.join(outfile_parts)

        # Save the syncerr log file overwriting any existing file
        with open(syncerr_fn, 'w') as txt_f:
            # In order to control the exact yaml layout for maximum readability
            # just write the lines as desired instead of using a yaml processor

            # Preamble
            txt_f.write('%YAML 1.2\n---\n')

            # General Info
            txt_f.write('{:<20}: "{}"\n'.format('fileName', os.path.split(outfile)[1]))
            txt_f.write('{:<20}: {}\n'.format('fileSize', best_size))
            txt_f.write('{:<20}: {} ({})\n'.format('tivoName', tivo_name, self.tivoIP))
            txt_f.write('{:<20}: {:%Y-%m-%dT%H:%M:%SZ}\n'.format('downloadStarted', datetime.utcfromtimestamp(best_start_time)))
            txt_f.write('{:<20}: {}\n'.format('attemptSaved', best_ndx + 1))
            txt_f.write('{:<20}: {}\n'.format('totalErrorPackets', best_error_packet_count))

            # download attempts
            txt_f.write('downloadAttempts:\n')
            for attempt_number, attempt in enumerate(download_attempts, start=1):
                transfer = {'size': attempt['size'],
                            'time': attempt['download_time'],
                           }
                transfer['mbps'] = prefix_bin_qty(transfer['size'] * 8.0 / transfer['time']);

                txt_f.write('    - {:<14}: {}\n'.format('attemptNumber', attempt_number))
                txt_f.write('      {:<14}: {}\n'.format('status', attempt['status']))
                txt_f.write('      {:<14}: {{ bytes: {size:>11}, seconds: {time:>6.1f}, rate: "{mbps[0]:6.2f} {mbps[1]}b/s" }}\n'
                            .format('transfer', **transfer))
                error_packets = attempt.get('error_packets', [])
                if error_packets:
                    txt_f.write('      errorPackets:\n')
                    for pkt_grp in error_packets:
                        txt_f.write('          - {{ count: {count:>6}, start: {start:>11}, end: {end:>11}, startMB: {startMB:>8.2f} }}\n'
                                    .format(**pkt_grp, startMB=pkt_grp['start'] / (1024 * 1024)))

            # yaml document end marker
            txt_f.write('...\n')

        logger.debug('Sync error log yaml file saved: %s', syncerr_fn)


    def get_out_file(self, status):
        """
        Get the full file path for the tivo recording to be downloaded (status['url'].
        The returned path will be to a non existent file.
        """

        fn_fmt_info = self.tivo_tasks['fn_format_info']
        togo_path = self.tivo_tasks['dest_path']
        showinfo = status['showinfo']
        decode = status['decode']
        ts_format = status['ts_format']

        fn_fmt = fn_fmt_info['movie'] if showinfo.is_movie() else fn_fmt_info['episode']

        # if the showinfo doesn't have a title, there's probably more info missing
        # so use the old style naming, or if there was no format specified in the
        # config by the user for this download type also fall back to old style naming.
        if not showinfo['title'] or not fn_fmt:
            return self.get_out_file_old(status, togo_path)

        file_ext = '.tivo'
        if decode:
            file_ext = '.ts' if ts_format else '.mpg'

        file_parts = {'title':             showinfo['title'],
                      'season':            showinfo['season_number'],
                      'episode':           showinfo['episode_number'],
                      'episode_title':     showinfo['episode_title'],
                      'movie_year':        showinfo['movie_year'],
                      'date_recorded':     showinfo['capture_date'],
                      'original_air_date': showinfo['original_air_date'],
                      'callsign':          showinfo['station_callsign'],
                      'channel':           showinfo['station_channel'],
                      'tivo_stream_type':  'TS' if ts_format else 'PS',
                     }

        # Convert the recorded datetime from UTC to local time. (default to the current date/time)
        if file_parts['date_recorded']:
            file_parts['date_recorded'] = file_parts['date_recorded'].astimezone(get_localzone())
        else:
            file_parts['date_recorded'] = datetime.now()

        # If the original air date is unknown, default it to Jan 1 1900
        if not file_parts['original_air_date']:
            file_parts['original_air_date'] = datetime(1900, 1, 1)

        filename = fn_fmt.format(**file_parts)

        # replace characters we don't want to allow in filenames
        for ch in BADCHAR:
            filename = filename.replace(ch, BADCHAR[ch])

        # make sure that the filepath we return is to a non-existent file
        count = 1
        full_name = [filename, '', file_ext]
        while True:
            filepath = os.path.join(togo_path, ''.join(full_name))
            if not os.path.isfile(filepath):
                break
            count += 1
            full_name[1] = ' ({})'.format(count)

        return filepath


    @staticmethod
    def get_out_file_old(status, togo_path):
        """
        Get the full file path for the tivo recording to be downloaded (status['url'].
        The returned path will be to a non existent file.

        This function provides TivoDesktop filenaming and the previous pyTivo file
        naming.
        """

        url = status['url']
        showinfo = status['showinfo']
        decode = status['decode']
        ts_format = status['ts_format']
        sortable = status['sortable']

        # Use TiVo Desktop style naming
        if showinfo['title']:
            title = showinfo['title']
            episodeTitle = showinfo['episode_title']
            recordDate = showinfo['capture_date']
            if recordDate:
                recordDate = recordDate.astimezone(get_localzone())
            else:
                recordDate = datetime.now()
            callsign = showinfo['station_callsign']

            fileParts = {'title':           title,
                         'recordDate':      recordDate,
                         'episodeTitle':    " - ''{}''".format(episodeTitle) if episodeTitle else '',
                         'callsign':        ', {}'.format(callsign) if callsign else '',
                         'tivo_stream_type': '',
                        }

            if decode:
                fileExt = '.ts' if ts_format else '.ps'
            else:
                fileExt = '.tivo'
                fileParts['tivo_stream_type'] = ' (TS)' if ts_format else ' (PS)'

            fnFmt = "{title}{episodeTitle} (Recorded {recordDate:%b %d, %Y}{callsign}){tivo_stream_type}"
            if sortable:
                fnFmt = "{title} - {recordDate:%Y-%m-%d}{episodeTitle}{callSign}{tivo_stream_type}"
                fileParts['callsign'] = ' ({})'.format(callsign) if callsign else ''

            fileName = fnFmt.format(**fileParts)

            for ch in BADCHAR:
                fileName = fileName.replace(ch, BADCHAR[ch])

            count = 1
            fullName = [fileName, '', fileExt]
            while True:
                filePath = os.path.join(togo_path, ''.join(fullName))
                if not os.path.isfile(filePath):
                    break
                count += 1
                fullName[1] = ' ({})'.format(count)

            return filePath

        # If we get here then use old style naming
        split_url = urlsplit(url)

        name = unquote(split_url[2]).split('/')[-1].split('.')
        try:
            tivo_item_id = unquote(split_url[3]).split('id=')[1]
            name.insert(-1, ' - ' + tivo_item_id)
        except:                             # pylint: disable=bare-except
            pass
        if decode:
            if ts_format:
                name[-1] = 'ts'
            else:
                name[-1] = 'mpg'
        else:
            if ts_format:
                name.insert(-1, ' (TS)')
            else:
                name.insert(-1, ' (PS)')

        nameHold = name
        name.insert(-1, '.')

        count = 2
        newName = name
        while os.path.isfile(os.path.join(togo_path, ''.join(newName))):
            newName = nameHold
            newName.insert(-1, ' (%d)' % count)
            newName.insert(-1, '.')
            count += 1

        name = newName
        name = ''.join(name)
        for ch in BADCHAR:
            name = name.replace(ch, BADCHAR[ch])

        return os.path.join(togo_path, name)


#
# TivoDownload exception errors
#

class Error(Exception):
    pass

#
# CONSTANTS
#

# Characters to remove from filenames and what to replace them with
BADCHAR = {'\\': '-',
           '/': '-',
           ':': ' -',
           ';': ',',
           '*': '.',
           '?': '.',
           '!': '.',
           '"': "'",
           '<': '(',
           '>': ')',
           '|': ' ',
          }

TS_PACKET_SIZE = 188
TS_PACKET_SYNC_BYTE = 0x47


#
# Local helper functions
#

def packets_with_sync_loss(buf):
    """
    Find all the packets with sync loss in the given buffer and return
    their location as a list of tuples with the (start_packet, count)
    """
    assert buf
    assert len(buf) % TS_PACKET_SIZE == 0
    sync_loss = False
    packets_lost = []
    for packet in range(len(buf) // TS_PACKET_SIZE):
        if buf[packet * TS_PACKET_SIZE] != TS_PACKET_SYNC_BYTE:
            if not sync_loss:
                sync_loss = True
                sync_loss_start = packet
        else:
            if sync_loss:
                sync_loss = False
                packets_lost.append((sync_loss_start, packet - sync_loss_start))

    if sync_loss:
        packets_lost.append((sync_loss_start, packet - sync_loss_start + 1)) # pylint: disable=undefined-loop-variable

    return packets_lost



mswindows = (sys.platform == "win32")

if mswindows:
    def _prevent_computer_from_sleeping(prevent=True):
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        # SetThreadExecutionState returns 0 when failed, which is ignored. The function should be supported from windows XP and up.
        if prevent:
            logger.info('PC sleep has been disabled')
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        else:
            logger.info('PC sleep has been enabled')
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

else:
    def _prevent_computer_from_sleeping(prevent=True):
        # pylint: disable=unused-argument
        # No preventComputerFromSleeping for MacOS and Linux yet.
        pass
