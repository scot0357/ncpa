from __future__ import division
import subprocess
import os
import ConfigParser
import logging
import shlex
import urllib2, urllib, urlparse
import tempfile
import pickle
import time
import re
from io import open
from itertools import izip


def get_cmdline(plugin_name, plugin_args, instruction):
    u"""Execute with special instructions.

    EXAMPLE instruction (Powershell):
    powershell -ExecutionPolicy Unrestricted $plugin_name $plugin_args

    EXAMPLE instruction (VBS):
    wscript $plugin_name $plugin_args

    """  
    command = []
    for x in shlex.split(instruction):
        if u'$plugin_name' == x:
            command.append(plugin_name)
        elif u'$plugin_args' == x and plugin_args:
            for y in plugin_args:
                command.append(y)
        else:
            command.append(x)
    return command


def standardize_log_check(accessor_response):
    '''Put the API log output into the standard form the plugin checker
    expects.

    '''
    try:
        logs = accessor_response['logs']
    except KeyError:
        logging.error('Trying to standardize log check but no logs key exists in response.')
        raise

    # We are going to create a summed values array. In order to keep the ordering
    # of the logs we are going to put them into the array based on lexical ordering
    # of the array keys.
    summed_values = []
    log_unit = ''
    log_names = sorted(logs.keys())

    for log_name in log_names:
        num_logs = len(logs[log_name])
        summed_values.append(num_logs)

    return log_names, {'logs': [summed_values, log_unit]}


def deltaize_call(key_name, result):
    """Saves the results from this run of the check to be checked later.

    """
    #Get our temp file filename to save our results too.
    filename = "ncpa-%s.tmp" % unicode(hash(key_name))
    tmpfile = os.path.join(tempfile.gettempdir(), filename)

    if os.path.isfile(tmpfile):
        #If the file exists, we extract the data from it and save it to our loaded_result
        #variable.
        result_file = open(tmpfile, u'r')
        loaded_result = pickle.load(result_file)
        result_file.close()
        last_modified = os.path.getmtime(tmpfile)
    else:
        #Otherwise load the loaded_result and last_modified with values that will cause zeros
        #to show up.
        loaded_result = result
        last_modified = 0

    #Update the pickled data
    logging.debug(u'Updating pickle for %s. filename is %s.' % (key_name, tmpfile))
    result_file = open(tmpfile, u'w')
    pickle.dump(result, result_file)
    result_file.close()

    #Calcluate the return value and return it
    delta = time.time() - last_modified
    return [abs((x - y) / delta) for x, y in izip(loaded_result, result)]


def make_plugin_response_from_accessor(accessor_response, accessor_args):
    """TODO: This function is a monster and needs to be broken up and rewritten

    """
    #~ First look at the GET and POST arguments to see what we are
    #~ going to use for our warning/critical
    is_log_check = False

    try:
        processed_args = dict(urlparse.parse_qsl(accessor_args))
    except ValueError:
        logging.debug('No argument detected in string %s', accessor_args)
        processed_args = {}
    except Exception, e:
        processed_args = {}
        logging.exception(e)
        logging.warning('Unabled to process arguments.')
    logging.error(accessor_response)

    #~ We need to have [{dictionary: value}] structure, so if it isn't that
    #~ we need to throw a warning
    #if 'logs' in accessor_response:
        #is_log_check = True
        #log_names, accessor_response = standardize_log_check(accessor_response)
    if type(list(accessor_response.values())[0]) is dict:
        stdout = 'ERROR: Non-node value requested.'
        returncode = 3
    else:
        result = list(accessor_response.values())[0]
        if not type(result) in [list, tuple]:
            unit = ''
            result = [result]
        try:
            unit = result[1]
        except IndexError:
            unit = ''
        result = result[0]

        if type(result) == bool:
            bool_name = list(accessor_response.keys())[0]
            if result:
                return {u'returncode': 0, u'stdout': u"%s's status was as expected." % bool_name}
            else:
                return {u'returncode': 2, u'stdout': u"%s's status was not as expected." % bool_name}
        elif not type(result) in [list, tuple]:
            result = [result]

        warning = processed_args.get(u'warning')
        critical = processed_args.get(u'critical')
        s_unit = processed_args.get(u'unit')
        delta = processed_args.get(u'delta')

        if delta:
            result = deltaize_call(list(accessor_response.keys())[0], result)

        if s_unit == u'T':
            factor = 1e12
        elif s_unit == u'G':
            factor = 1e9
        elif s_unit == u'M':
            factor = 1e6
        elif s_unit == u'K':
            factor = 1e3
        else:
            factor = 1
        if u'm' in unit and s_unit:
            factor *= 1e3
        result = [round(x/factor, 3) for x in result]
        try:
            warn_lat = [is_within_range(warning, x) for x in result]
            crit_lat = [is_within_range(critical, x) for x in result]
        except:
            return {u'returncode': 3, u'stdout': u'Bad Nagios range values'}
        if s_unit:
            unit = s_unit + unit.replace(u'm', u'')
        if any(crit_lat):
            returncode = 2
            prefix = u'CRITICAL'
        elif any(warn_lat):
            returncode = 1
            prefix = u'WARNING'
        else:
            returncode = 0
            prefix = u'OK'
        label = list(accessor_response.keys())[0]
        name = label.capitalize()
        stdout = u"%s: %s was " % (prefix, name)
        stdout = stdout.replace(u'|', u'/')
        if delta:
            psec = u'/sec'
        else:
            psec = u''
        stdout += u",".join([unicode(x) + unit + psec for x in result])
        perfdata = []
        count = 0
        for x in result:
            tmplabel = u"%s_%d" % (label, count)
            count += 1
            pdata = u"'%s'=%s%s" % (tmplabel, unicode(x),  unit)
            if warning:
                pdata += u';%s' % warning
            if critical:
                if not warning:
                    pdata += u';'
                pdata += u';%s' % critical
            perfdata.append(pdata)
        perfdata = u' '.join(perfdata)
        stdout = u"%s|%s" % (stdout, perfdata)

    return {u'returncode': returncode, u'stdout': stdout}


def is_within_range(nagios_range, value):
    """Returns False if the given value will raise an alert for the given
    nagios_range.

    """
    #First off, we must ensure that the range exists, otherwise just return (not warning or critical.)
    if not nagios_range:
        return False

    #Next make sure the value is a number of some sort
    value = float(value)

    #Setup our regular expressions to parse the Nagios ranges
    first_float = r'(?P<first>(-?[0-9]+(\.[0-9]+)?))'
    second_float = r'(?P<second>(-?[0-9]+(\.[0-9]+)?))'

    #The following is a list of regular expression => function. If the regular expression matches
    #then run the function. The function is a comparison involving value.
    actions = [(r'^%s$' % first_float, lambda y: (value > float(y.group('first'))) or (value < 0)),
               (r'^%s:$' % first_float, lambda y: value < float(y.group('first'))),
               (r'^~:%s$' % first_float, lambda y: value > float(y.group('first'))),
               (r'^%s:%s$' % (first_float, second_float), lambda y: (value < float(y.group('first'))) or (value > float(y.group('second')))),
               (r'^@%s:%s$' % (first_float, second_float), lambda y: not((value < float(y.group('first'))) or (value > float(y.group('second')))))]

    #For each of the previous list items, run the regular expression, and if the regular expression
    #finds a match, run the function and return its comparison result.
    for regex_string, func in actions:
        res = re.match(regex_string, nagios_range)
        if res:
            return func(res)

    #If none of the items matches, the warning/critical format was bogus! Sound the alarms!
    raise Exception('Improper warning/critical format.')


def get_plugin_instructions(plugin_name, config):
    u"""Returns the instruction to use for the given plugin.
    If nothing exists for the suffix, then simply return the basic

    $plugin_name $plugin_args

    """
    _, extension = os.path.splitext(plugin_name)
    try:
        return config.get(u'plugin directives', extension)
    except ConfigParser.NoOptionError:
        return u'$plugin_name $plugin_args'


def execute_plugin(plugin_name, plugin_args, config):
    """Runs custom scripts that MUST be located in the scripts subdirectory
    of the executable

    """
    #Assemble our absolute plugin file name for calling
    plugin_path = config.get('plugin directives', 'plugin_path')
    plugin_abs_path = os.path.join(plugin_path, plugin_name)

    #Get any special instructions from the config for executing the plugin
    instructions = get_plugin_instructions(plugin_abs_path, config)

    #Make our command line
    cmd = get_cmdline(plugin_abs_path, plugin_args, instructions)

    logging.debug('Running process with command line: `%s`', ' '.join(cmd))

    running_check = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    running_check.wait()

    returncode = running_check.returncode
    stdout = ''.join(running_check.stdout.readlines()).replace('\r\n', '\n').replace('\r', '\n').strip()

    return {'returncode': returncode, 'stdout': stdout}

