#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Author:  Enrico Tröger
#          Frank Lanitz <frank@frank.uvena.de>
# License: GPL v2 or later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.


from yaturl import config
from yaturl.console.manager import ConsoleManager
from yaturl.database.database import YuDatabase
from yaturl.helpers.logger import get_access_logger, get_logger
from yaturl.server import YuServer
from yaturl.thread import YuServerThread
import yaturl.constants
from optparse import OptionParser
from signal import signal, SIGINT, SIGTERM
import daemon
import errno
import logging
import logging.config
import os
import pwd
import sys
from threading import Event


shutdown_event = Event()


#----------------------------------------------------------------------
def setup_options(base_dir, parser):
    """
    Set up options and defaults

    @param parser (optparse.OptionParser())
    """
    parser.add_option(
        '-c', dest='config',
        default='%s/etc/yaturl.conf' % base_dir,
        help=u'configuration file')
    parser.add_option(
        '-f', action='store_false',
        dest='daemonize',
        default=True,
        help=u'stay in foreground, do not daemonize')


#----------------------------------------------------------------------
def shutdown():
    logger = get_logger()
    logger.info(u'Initiating shutdown')
    shutdown_event.set()


#----------------------------------------------------------------------
def signal_handler(signum, frame):
    """
    On SIGTERM and SIGINT, trigger shutdown
    """
    logger = get_logger()
    logger.info(u'Received signal %s' % signum)
    shutdown()


#----------------------------------------------------------------------
def is_service_running(pid_file_path):
    """
    Check whether the service is already running

    | **param** pid_file_path (str)
    | **return** is_running (bool)
    """
    if os.path.exists(pid_file_path):
        pid_file = open(pid_file_path, 'r')
        pid = pid_file.read().strip()
        pid_file.close()
        if pid:
            try:
                pid = int(pid)
            except ValueError:
                return False
            # sending signal 0 fails if the process doesn't exist (anymore)
            # and won't do anything if the process is running
            try:
                os.kill(pid, 0)
            except OSError, e:
                if e.errno == errno.ESRCH:
                    return False
        return True
    return False


#----------------------------------------------------------------------
def setup_logging(options):
    logging.config.fileConfig(options.config)


#----------------------------------------------------------------------
def create_server_threads(logger, accesslog):

    def set_console_manager_locals():
        locals_ = dict(
            config=config,
            logger=logger,
            accesslog=accesslog,
            http_server=http_server,
            telnet_server=console_manager.get_telnet_server(),
            console_manager=console_manager,
            shutdown=shutdown,
            get_system_status=ConsoleManager.get_system_status)
        console_manager.set_locals(locals_)

    def create_telnet_server():
        if not config.getboolean('telnet', 'enable'):
            return None
        host = config.get('telnet', 'host')
        port = config.getint('telnet', 'port')
        console_manager = ConsoleManager(host, port)
        telnet_server_thread = YuServerThread(
            target=console_manager.serve_forever,
            name='Telnet Console Server',
            instance=console_manager)
        server_threads.append(telnet_server_thread)
        return console_manager

    def create_http_server():
        http_server = YuServer()
        http_server_thread = YuServerThread(
            target=http_server.serve_forever,
            name='HTTP Server',
            instance=http_server)
        server_threads.append(http_server_thread)
        return http_server

    server_threads = []

    http_server = create_http_server()
    console_manager = create_telnet_server()
    set_console_manager_locals()

    return server_threads


#----------------------------------------------------------------------
def watch_running_threads(running_threads, timeout=300):
    # watch running threads
    logger = get_logger()
    while not shutdown_event.isSet():
        for server_thread in running_threads:
            if not server_thread.isAlive():
                logger.error(u'Server thread "%s" died, shutting down' % server_thread.getName())
                shutdown_event.set()
        shutdown_event.wait(timeout)

    # stop remaining threads
    for server_thread in running_threads:
        if server_thread.isAlive():
            server_thread.shutdown()
            server_thread.join()


#----------------------------------------------------------------------
def main():
    """
    main()

    | **return** exit_code (int)
    """

    base_dir = os.path.abspath('%s/..' % (os.path.dirname(__file__)))

    # arguments
    option_parser = OptionParser()
    setup_options(base_dir, option_parser)
    arg_options = option_parser.parse_args()[0]

    # configuration
    if not os.path.exists(arg_options.config):
        raise RuntimeError(u'Configuration file does not exist')
    config.read(arg_options.config)

    # set uid
    if config.has_option('main', 'user'):
        name = config.get('main', 'user')
        uid = pwd.getpwnam(name)[2]
        os.setuid(uid)

    # daemonize
    if arg_options.daemonize:
        daemon.WORKDIR = base_dir
        daemon.createDaemon()

    # pid handling
    pid_file_path = config.get('main', 'pid_file_path')
    if is_service_running(pid_file_path):
        print >> sys.stderr, 'Already running'
        exit(1)
    pid = open(pid_file_path, 'w')
    pid.write(str(os.getpid()))
    pid.close()

    thread_watch_timeout = config.getint('main', 'thread_watch_timeout')

    # logging
    setup_logging(arg_options)
    accesslog = get_access_logger()
    logger = get_logger()
    logger.info('Application starts up')

    # handle signals
    signal(SIGINT,  signal_handler)
    signal(SIGTERM, signal_handler)

    # Checking for templates
    if config.has_option('templates', 'path'):
        for template in yaturl.constants.TEMPLATENAMES:
            tmp_path = config.get('templates', 'path') + template
            if os.path.exists(tmp_path):
                logger.info('Template %s seems to be available. Good.' % (template))
            else:
                logger.info('Template %s seems to be missing. '
                              'Aborting startup' % (template))
                # Maybe shutdown can be done a bit nicer.
                exit(1)

    # set up database
    YuDatabase.init_connection_pool()

    server_threads = create_server_threads(logger, accesslog)

    # start server threads
    for server_thread in server_threads:
        server_thread.start()
        logger.info('%s started' % server_thread.getName())

    watch_running_threads(server_threads, thread_watch_timeout)

    logger.info(u'Shutdown')

    # cleanup
    logging.shutdown()

    exit(0)


if __name__ == "__main__":
    main()