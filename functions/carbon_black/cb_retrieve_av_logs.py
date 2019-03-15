# -*- coding: utf-8 -*-
# pragma pylint: disable=unused-argument, no-self-use

# This function will retrieve the Microsoft Antimalware and/or Windows Defender AV logs from a sensor running Win7, Win8, or Win10.
# File: cb_retrieve_av_logs.py
# Date: 02/27/2019 - Modified: 03/15/2019
# Author: Jared F

"""Function implementation"""
#   @function -> cb_retrieve_av_logs
#   @params -> integer: incident_id, string: hostname
#   @return -> boolean: results['was_successful'], string: results['hostname']


import os
import time
import tempfile
import zipfile
import logging
import datetime
from resilient_circuits import ResilientComponent, function, handler, StatusMessage, FunctionResult, FunctionError
from cbapi.response import CbEnterpriseResponseAPI, Sensor
from cbapi.errors import TimeoutError
import carbon_black.util.selftest as selftest

cb = CbEnterpriseResponseAPI()  # CB Response API
MAX_TIMEOUTS = 3  # The number of CB timeouts that must occur before the function aborts
DAYS_UNTIL_TIMEOUT = 3  # The number of days that must pass before the function aborts

class FunctionComponent(ResilientComponent):
    """Component that implements Resilient function 'cb_retrieve_av_logs"""

    def __init__(self, opts):
        """constructor provides access to the configuration options"""
        super(FunctionComponent, self).__init__(opts)
        self.options = opts.get("carbon_black", {})
        selftest.selftest_function(opts)

    @handler("reload")
    def _reload(self, event, opts):
        """Configuration options have changed, save new values"""
        self.options = opts.get("carbon_black", {})

    @function("cb_retrieve_av_logs")
    def _cb_retrieve_av_logs_function(self, event, *args, **kwargs):

        results = {}

        try:
            # Get the function parameters:
            incident_id = kwargs.get("incident_id")  # number
            hostname = kwargs.get("hostname")  # text

            log = logging.getLogger(__name__)  # Establish logging

            days_later_timeout_length = datetime.datetime.now() + datetime.timedelta(days=DAYS_UNTIL_TIMEOUT)  # Max duration length before aborting
            hostname = (hostname.upper())[:15]  # CB limits hostname to 15 characters
            sensor = (cb.select(Sensor).where('hostname:' + hostname))  # Query CB for the hostname's sensor
            timeouts = 0  # Number of timeouts that have occurred

            if len(sensor) <= 0:  # Host does not have CB agent, abort
                yield StatusMessage("[FATAL ERROR] CB could not find hostname: " + str(hostname))
                results["was_successful"] = False
                yield FunctionResult(results)
                return

            sensor = sensor[0]  # Get the sensor object from the query
            results["hostname"] = str(hostname).upper()

            while timeouts <= MAX_TIMEOUTS:  # Max timeouts before aborting

                try:

                    now = datetime.datetime.now()

                    # Check if the sensor is queued to restart, wait up to 90 seconds before checking online status
                    three_minutes_passed = datetime.datetime.now() + datetime.timedelta(minutes=3)
                    while (sensor.restart_queued is True) and (three_minutes_passed >= now):
                        time.sleep(3)  # Give the CPU a break, it works hard!
                        now = datetime.datetime.now()
                        sensor = (cb.select(Sensor).where('hostname:' + hostname))[0]  # Retrieve the latest CB sensor vitals

                    # Check online status
                    if sensor.status != "Online":
                        yield StatusMessage('[WARNING] Hostname: ' + str(hostname) + ' is offline. Will attempt for 3 days...')
                    while (sensor.status != "Online") and (days_later_timeout_length >= now):  # Continuously check if the sensor comes online for 3 days
                        time.sleep(3)  # Give the CPU a break, it works hard!
                        now = datetime.datetime.now()
                        sensor = (cb.select(Sensor).where('hostname:' + hostname))[0]  # Retrieve the latest CB sensor vitals

                    # Abort after DAYS_UNTIL_TIMEOUT
                    if sensor.status != "Online":
                        yield StatusMessage('[FATAL ERROR] Hostname: ' + str(hostname) + ' is still offline!')
                        results["was_successful"] = False
                        yield FunctionResult(results)
                        return

                    # Check if the sensor is queued to restart, wait up to 90 seconds before continuing
                    three_minutes_passed = datetime.datetime.now() + datetime.timedelta(minutes=3)
                    while (sensor.restart_queued is True) and (three_minutes_passed >= now):  # If the sensor is queued to restart, wait up to 90 seconds
                        time.sleep(3)  # Give the CPU a break, it works hard!
                        now = datetime.datetime.now()
                        sensor = (cb.select(Sensor).where('hostname:' + hostname))[0]  # Retrieve the latest CB sensor vitals

                    # Verify the incident still exists and is reachable, if not abort
                    try: incident = self.rest_client().get('/incidents/{0}?text_content_output_format=always_text&handle_format=names'.format(str(incident_id)))
                    except Exception as err:
                        if err.message and "not found" in err.message.lower():
                            log.info('[FATAL ERROR] Incident ID ' + str(incident_id) + ' no longer exists.')
                            log.info('[FAILURE] Fatal error caused exit!')
                        else:
                            log.info('[FATAL ERROR] Incident ID ' + str(incident_id) + ' could not be reached, Resilient instance may be down.')
                            log.info('[FAILURE] Fatal error caused exit!')
                        return

                    # Establish a session to the host sensor
                    yield StatusMessage('[INFO] Establishing session to CB Sensor #' + str(sensor.id) + ' (' + sensor.hostname + ')')
                    session = cb.live_response.request_session(sensor.id)
                    yield StatusMessage('[SUCCESS] Connected on Session #' + str(session.session_id) + ' to CB Sensor #' + str(sensor.id) + ' (' + sensor.hostname + ')')

                    files_to_grab = []  # Stores log file path located for retrieval

                    try:  # Attempt to locate log files from Microsoft Antimalware
                        session.list_directory(r'C:\ProgramData\Microsoft\Microsoft Antimalware\Support')
                        av_log_path = r'C:\ProgramData\Microsoft\Microsoft Antimalware\Support'
                        files_to_grab += [av_log_path + '\\' + each_file['filename'] for each_file in session.list_directory(av_log_path + r'\mplog*') if 'DIRECTORY' not in each_file['attributes']]
                    except TimeoutError: raise
                    except Exception: pass

                    try:  # Attempt to locate log files from Windows Defender
                        session.list_directory(r'C:\ProgramData\Microsoft\Windows Defender\Support')
                        av_log_path = r'C:\ProgramData\Microsoft\Windows Defender\Support'
                        files_to_grab += [av_log_path + '\\' + each_file['filename'] for each_file in session.list_directory(av_log_path + r'\mplog*') if 'DIRECTORY' not in each_file['attributes']]
                    except TimeoutError: raise
                    except Exception: pass

                    if not files_to_grab:  # No log files were located for retrieval, abort
                        yield StatusMessage('[FATAL ERROR] Could not find a valid AV log path with logs on Sensor!')
                        results["was_successful"] = False
                        yield FunctionResult(results)
                        return

                    with tempfile.NamedTemporaryFile(delete=False) as temp_zip:  # Create temporary temp_zip for creating zip_file
                        try:
                            with zipfile.ZipFile(temp_zip, 'w') as zip_file:  # Establish zip_file from temporary temp_zip for packaging logs into
                                for each_file in files_to_grab:  # For each located log file
                                    with tempfile.NamedTemporaryFile(delete=False) as temp_file:  # Create temp_file for log
                                        try:
                                            file_name = r'{0}-{1}.txt'.format(sensor.hostname, os.path.basename(each_file.replace('\\', os.sep)))
                                            temp_file.write(session.get_file(each_file))  # Write the log to temp_file
                                            temp_file.close()
                                            zip_file.write(temp_file.name, file_name, compress_type=zipfile.ZIP_DEFLATED)  # Write temp_file into zip_file
                                            log.info('[INFO] Retrieved: ' + each_file)
                                        finally:
                                            os.unlink(temp_file.name)  # Delete temporary temp_file
                            self.rest_client().post_attachment('/incidents/{0}/attachments'.format(incident_id), temp_zip.name, '{0}-AV_Logs.zip'.format(sensor.hostname))  # Post zip_file to incident
                            yield StatusMessage('[SUCCESS] Posted ZIP file of AV logs to the incident as an attachment!')

                        finally:
                            os.unlink(temp_zip.name)  # Delete temporary temp_zip

                except TimeoutError:  # Catch TimeoutError and handle
                    timeouts = timeouts + 1
                    if timeouts <= MAX_TIMEOUTS: yield StatusMessage('[ERROR] TimeoutError was encountered. Reattempting... (' + str(timeouts) + '/3)')
                    else:
                        yield StatusMessage('[FATAL ERROR] TimeoutError was encountered. The maximum number of retries was reached. Aborting!')
                        yield StatusMessage('[FAILURE] Fatal error caused exit!')
                        results["was_successful"] = False
                    try: session.close()
                    except: pass
                    sensor = (cb.select(Sensor).where('hostname:' + hostname))[0]  # Retrieve the latest CB sensor vitals
                    sensor.restart_sensor()  # Restarting the sensor may avoid a timeout from occurring again
                    time.sleep(30)  # Sleep to apply sensor restart
                    sensor = (cb.select(Sensor).where('hostname:' + hostname))[0]  # Retrieve the latest CB sensor vitals
                    continue

                except Exception as err:  # Catch all other exceptions and abort
                    yield StatusMessage('[FATAL ERROR] Encountered: ' + str(err))
                    yield StatusMessage('[FAILURE] Fatal error caused exit!')
                    results["was_successful"] = False
                
                else:
                    results["was_successful"] = True

                try: session.close()
                except: pass
                yield StatusMessage('[INFO] Session has been closed to CB Sensor #' + str(sensor.id) + '(' + sensor.hostname + ')')
                break

            # Produce a FunctionResult with the results
            yield FunctionResult(results)
        except Exception:
            yield FunctionError()
