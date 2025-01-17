import base64
import gzip
import json
import logging
import os
import re
from io import BytesIO

from python3.shipper.shipper import LogzioShipper

KEY_INDEX = 0
VALUE_INDEX = 1
LOG_LEVELS = ['alert', 'trace', 'debug', 'notice', 'info', 'warn',
              'warning', 'error', 'err', 'critical', 'crit', 'fatal',
              'severe', 'emerg', 'emergency']

PYTHON_EVENT_SIZE = 3
NODEJS_EVENT_SIZE = 4
LAMBDA_LOG_GROUP = '/aws/lambda/'

# comprehensive pattern to match IPv4 and IPv6 addresses
IP_PATTERN = re.compile(
    '(?<![:.\w])(?:(?:[0-9]{1,3}\.){3}[0-9]{1,3}|(?:[0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}|(?:[0-9a-fA-F]{1,4}:){1,7}:|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:(?:(?::[0-9a-fA-F]{1,4}){1,6})|:(?:(?::[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(?::[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::(?:ffff(?::0{1,4}){0,1}:){0,1}(?:(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9])|(?:[0-9a-fA-F]{1,4}:){1,4}:(?:(?:25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9]))(?![:.\w])',
    re.IGNORECASE
)


# set logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _extract_aws_logs_data(event):
    # type: (dict) -> dict
    event_str = event['awslogs']['data']
    try:
        logs_data_decoded = base64.b64decode(event_str)
        logs_data_unzipped = gzip.GzipFile(fileobj=BytesIO(logs_data_decoded))
        logs_data_unzipped = logs_data_unzipped.read()
        logs_data_dict = json.loads(logs_data_unzipped)
        return logs_data_dict
    except ValueError as e:
        logger.error("Got exception while loading json, message: {}".format(e))
        raise ValueError("Exception: json loads")


def _extract_lambda_log_message(log):
    # type: (dict) -> None
    str_message = str(log['message'])
    try:
        start_level = str_message.index('[')
        end_level = str_message.index(']')
        log_level = str_message[start_level + 1:end_level]
        if log_level.lower() in LOG_LEVELS:
            log['log_level'] = log_level
            start_split = end_level + 2
        else:
            start_split = 0
    except ValueError:
        # Let's try without log level
        start_split = 0
    message_parts = str_message[start_split:].split('\t')
    size = len(message_parts)
    if size == PYTHON_EVENT_SIZE or size == NODEJS_EVENT_SIZE:
        log['@timestamp'] = message_parts[0]
        log['requestID'] = message_parts[1]
        log['message'] = message_parts[size - 1]
    if size == NODEJS_EVENT_SIZE:
        log['log_level'] = message_parts[2]


def _add_timestamp(log):
    # type: (dict) -> None
    if '@timestamp' not in log:
        log['@timestamp'] = str(log['timestamp'])
        del log['timestamp']


def _parse_to_json(log):
    # type: (dict) -> None
    try:
        if os.environ['FORMAT'].lower() == 'json':
            json_object = json.loads(log['message'])
            for key, value in json_object.items():
                log[key] = value
    except (KeyError, ValueError) as e:
        pass


def _anonymize_ip_addresses(log):
    # type: (dict) -> None
    msg = str(log['message'])

    last_end = 0
    new_msg = ""
    matches = IP_PATTERN.finditer(msg)
    for match in matches:
        new_msg = new_msg + msg[last_end:match.start()]
        new_msg = new_msg + 'IP(' + str(hash(match.group())) + ')'
        last_end = match.end()
    new_msg = new_msg + msg[last_end:]
    log['message'] = new_msg


def _parse_cloudwatch_log(log, additional_data):
    # type: (dict, dict) -> bool
    _add_timestamp(log)
    _anonymize_ip_addresses(log)
    if LAMBDA_LOG_GROUP in additional_data['service']:
        if _is_valid_log(log):
            _extract_lambda_log_message(log)
        else:
            return False
    del log['id']
    log.update(additional_data)
    _parse_to_json(log)
    return True


def _get_additional_logs_data(aws_logs_data, context):
    # type: (dict, 'LambdaContext') -> dict
    additional_data = {
        'service': aws_logs_data['logGroup'],
        'logger_name': aws_logs_data['logStream']
    }
    try:
        # If ENRICH has value, add the properties
        if os.environ['ENRICH']:
            properties_to_enrich = os.environ['ENRICH'].split(";")
            for property_to_enrich in properties_to_enrich:
                property_key_value = property_to_enrich.split("=")
                additional_data[property_key_value[KEY_INDEX]] = property_key_value[VALUE_INDEX]
    except KeyError:
        pass

    try:
        additional_data['type'] = os.environ['TYPE']
    except KeyError:
        logger.info("Using default TYPE 'logzio_cloudwatch_lambda'.")
        additional_data['type'] = 'logzio_cloudwatch_lambda'
    return additional_data


def _is_valid_log(log):
    # type (dict) -> bool
    message = log['message']
    is_info_log = message.startswith('START') or message.startswith('END') or message.startswith('REPORT')
    return not is_info_log


def lambda_handler(event, context):
    # type (dict, 'LambdaContext') -> None

    aws_logs_data = _extract_aws_logs_data(event)
    additional_data = _get_additional_logs_data(aws_logs_data, context)
    shipper = LogzioShipper()

    logger.info("About to send {} logs".format(len(aws_logs_data['logEvents'])))
    for log in aws_logs_data['logEvents']:
        if not isinstance(log, dict):
            raise TypeError("Expected log inside logEvents to be a dict but found another type")
        if _parse_cloudwatch_log(log, additional_data):
            shipper.add(log)

    shipper.flush()
