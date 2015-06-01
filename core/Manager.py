# -*- coding: utf-8 -*-
"""
This file contains the QuDi Manager class.

QuDi is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

QuDi is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with QuDi. If not, see <http://www.gnu.org/licenses/>.

Copyright (C) 2015 Jan M. Binder jan.binder@uni-ulm.de

Derived form ACQ4:
Copyright 2010  Luke Campagnola
Originally distributed under MIT/X11 license. See documentation/MITLicense.txt for more infomation.
"""

import os
import sys
import gc
import getopt
import glob
import re
import time
import atexit
import weakref
import importlib
import threading

from pyqtgraph.Qt import QtCore, QtGui
import pyqtgraph.reload as reload
import pyqtgraph.configfile as configfile

from .util import ptime
from .util.Mutex import Mutex   # Mutex provides access serialization between threads
from collections import OrderedDict
import pyqtgraph as pg
from .Logger import Logger, LOG, printExc
from .ThreadManager import ThreadManager
from .Remote import RemoteObjectManager
from .Base import Base

class Manager(QtCore.QObject):
    """The Manager object is responsible for:
      - Loading/configuring device modules and storing their handles
      - Providing unified timestamps
      - Making sure all devices/modules are properly shut down
        at the end of the program

      @signal sigConfigChanged: the configuration has changed, please reread your configuration
      @signal sigModulesChanged: the available modules have changed
      @signal sigModuleHasQuit: the module whose name is passed is now deactivated
      @signal sigBaseDirChanged: the base directiory has changed
      @signal sigAbortAll: abort all running things as quicly as possible
      @signal sigManagerQuit: the manager is quitting
      @signal sigManagerShow: show whatever part of the GUI is important
      """
      

    # Prepare Signal declarations for Qt: Allows Python to interface with Qt 
    # signal and slot delivery mechanisms.
    sigConfigChanged = QtCore.Signal()
    sigModulesChanged = QtCore.Signal() 
    sigModuleHasQuit = QtCore.Signal(object)
    sigBaseDirChanged = QtCore.Signal()
    sigLogDirChanged = QtCore.Signal(object)
    sigAbortAll = QtCore.Signal()
    sigManagerQuit = QtCore.Signal(object)
    sigShowManager = QtCore.Signal()
    sigShowLog = QtCore.Signal()
    sigShowConsole = QtCore.Signal()
    
    def __init__(self, configFile=None, argv=None):
        """Constructor for QuDi main management class

          @param string configFile: path to configuration file
          @param list argv: command line arguments
        """
        # used for keeping some basic methods thread-safe
        self.lock = Mutex(recursive=True)
        self.tree = OrderedDict()
        self.tree['config'] = OrderedDict()
        self.tree['start'] = OrderedDict()
        self.tree['defined'] = OrderedDict()
        self.tree['loaded'] = OrderedDict()
        
        self.tree['defined']['hardware'] = OrderedDict()
        self.tree['loaded']['hardware'] = OrderedDict()
        
        self.tree['start']['gui'] = OrderedDict()
        self.tree['defined']['gui'] = OrderedDict()
        self.tree['loaded']['gui'] = OrderedDict()
        
        self.tree['start']['logic'] = OrderedDict()
        self.tree['defined']['logic'] = OrderedDict()
        self.tree['loaded']['logic'] = OrderedDict()

        self.currentDir = None
        self.baseDir = None
        self.disableDevs = []
        self.disableAllDevs = False
        self.alreadyQuit = False

        try:
            # Logging
            global LOG
            LOG = Logger(self)
            #print(LOG)
            self.logger = LOG
            
            # Command Line parameters
            if argv is not None:
                try:
                    opts, args = getopt.getopt(argv, 'c:a:m:b:s:d:nD',
                    ['config=',
                    'config-name=',
                    'module=',
                    'base-dir=',
                    'storage-dir=',
                    'disable=',
                    'no-manager',
                    'disable-all'])
                except getopt.GetoptError as err:
                    print(str(err))
                    print("""
    Valid options are:
        -c --config=       Configuration file to load
        -a --config-name=  Named configuration to load
        -m --module=       Module name to load
        -b --base-dir=     Base directory to use
        -s --storage-dir=  Storage directory to use
        -n --no-manager    Do not load manager module
        -d --disable=      Disable the device specified
        -D --disable-all   Disable all devices
    """)
                    raise
            else:
                opts = []
            
            # Initialize parent class QObject
            QtCore.QObject.__init__(self)
            atexit.register(self.quit)

            # Thread management
            self.tm = ThreadManager()
            self.tm.sigLogMessage.connect(self.logger.queuedLogMsg)
            self.logger.logMsg('Main thread is {0}'.format(threading.get_ident()), msgType='thread')
            #mthread = self.tm.newThread('manager')
            #self.moveToThread(mthread)
            #mthread.start()
            
            # Handle command line options
            loadModules = []
            setBaseDir = None
            setStorageDir = None
            loadManager = True
            loadConfigs = []
            for o, a in opts:
                if o in ['-c', '--config']:
                    configFile = a
                elif o in ['-a', '--config-name']:
                    loadConfigs.append(a)
                elif o in ['-m', '--module']:
                    loadModules.append(a)
                elif o in ['-b', '--baseDir']:
                    setBaseDir = a
                elif o in ['-s', '--storageDir']:
                    setStorageDir = a
                elif o in ['-n', '--noManager']:
                    loadManager = False
                elif o in ['-d', '--disable']:
                    self.disableDevs.append(a)
                elif o in ['-D', '--disable-all']:
                    self.disableAllDevs = True
                else:
                    print("Unhandled option", o, a)
            
            # Read in configuration file
            if configFile is None:
                configFile = self._getConfigFile()
            
            self.configDir = os.path.dirname(configFile)
            self.readConfig(configFile)

            self.rm = RemoteObjectManager(self.tm, self.logger)
            self.rm.createServer(12345)

            self.logger.logMsg('QuDi started.', importance=9)
            
            # Act on options if they were specified..
            try:
                for name in loadConfigs:
                    self.loadDefinedConfig(name)
                for m in loadModules:
                    try:
                        self.loadDefinedModule(m)
                    except:
                        raise
            except:
                printExc('\n: Error while acting on command line options: '
                         '(but continuing on anyway..)')
            # Load startup things from config here
            for key in self.tree['start']['gui']:
                try:
                    modObj = self.importModule('gui', self.tree['start']['gui'][key]['module'])
                    pkgName = re.escape(modObj.__package__)
                    modName = re.sub('^{0}\.'.format(pkgName), '', modObj.__name__)
                    modName = modObj.__name__.replace(modObj.__package__, '').replace('.', '')
                    
                    self.configureModule(modObj, 'gui', modName, key, self.tree['start']['gui'][key])
                    self.tree['loaded']['gui'][key].activate()
                except:
                    raise
            # Configuration has changed with activation
            self.sigModulesChanged.emit()
        except:
            printExc("Error while configuring Manager:")
        finally:
            if len(self.tree['loaded']['logic']) == 0 and len(self.tree['loaded']['gui']) == 0 :
                self.logger.logMsg('No modules loaded during startup. Not '
                                   'is happening.', importance=9)
    def getMainDir(self):
        """Returns the absolut path to the directory of the main software.
        
             @return string: path to the main tree of the software
        
        """ 
        return os.path.abspath( os.path.join( os.path.dirname(__file__), ".." ) )

    def _getConfigFile(self):
        """ Search all the default locations to find a configuration file.
          
          @return sting: path to configuration file
        """
        from . import CONFIGPATH
        for path in CONFIGPATH:
            cf = os.path.join(path, 'custom.cfg')
            if os.path.isfile(cf):
                return cf
            cf = os.path.join(path, 'default.cfg')
            if os.path.isfile(cf):
                return cf
        raise Exception("Could not find config file in: {0}".format(CONFIGPATH) )
    
    def _appDataDir(self):
        """Get the system specific application data directory.

          @return string: path to application directory
        """
        # return the user application data directory
        if sys.platform == 'win32':
            # resolves to "C:/Documents and Settings/User/Application Data/"
            # on XP and "C:\User\Username\AppData\Roaming" on win7
            return os.path.join(os.environ['APPDATA'], 'qudi')
        elif sys.platform == 'darwin':
            return os.path.expanduser('~/Library/Preferences/qudi')
        else:
            return os.path.expanduser('~/.local/qudi')

            
    def readConfig(self, configFile):
        """Read configuration file and sort entries into categories.

          @param string configFile: path to configuration file
        """
        print("============= Starting Manager configuration from {0} =================".format(configFile) )
        self.logger.logMsg("Starting Manager configuration from {0}".format(configFile) )
        cfg = configfile.readConfigFile(configFile)
            
        # Read modules, devices, and stylesheet out of config
        self.configure(cfg)

        self.configFile = configFile
        print("\n============= Manager configuration complete =================\n")
        self.logger.logMsg('Manager configuration complete.')
        
    def configure(self, cfg):
        """Sort modules from configuration into categories

          @param dict cfg: dictionary from configuration file

          There are the main categories hardware, logic, gui, startup
          and global.
          Startup modules can be logic or gui and are loaded
          directly on 'startup'.
          'global' contains settings for the whole application.
          hardware, logic and gui contain configuration of and 
          for loadable modules.
        """
        
        for key in cfg:
            try:
                # hardware
                if key == 'hardware':
                    for m in cfg['hardware']:
                        if self.disableAllDevs or m in self.disableDevs:
                            self.logger.print_logMsg("    --> Ignoring device {0} -- disabled by request".format(m) )
                            continue
                        if 'module' in cfg['hardware'][m]:
                            self.tree['defined']['hardware'][m] = cfg['hardware'][m]
                        else: 
                            self.logger.print_logMsg("    --> Ignoring device {0} -- no module specified".format(m))

                # logic
                elif key == 'logic':
                    for m in cfg['logic']:
                        if 'module' in cfg['logic'][m]:
                            self.tree['defined']['logic'][m] = cfg['logic'][m]
                        else:
                            self.logger.print_logMsg("    --> Ignoring logic {0} -- no module specified".format(m) )
                        
                # GUI
                elif key == 'gui':
                    for m in cfg['gui']:
                        if 'module' in cfg['gui'][m]:
                            self.tree['defined']['gui'][m] = cfg['gui'][m]
                        else:
                            self.logger.print_logMsg("    --> Ignoring GUI {0} -- no module specified".format(m) )

                # Load on startup
                elif key == 'startup':
                    for skey in cfg['startup']:
                        if skey == 'gui':
                            for m in cfg['startup']['gui']:
                                if 'module' in cfg['startup']['gui'][m]:
                                    self.tree['start']['gui'][m] = cfg['startup']['gui'][m]
                                else:
                                    self.logger.print_logMsg("    --> Ignoring startup logic {0} -- no module specified".format(m) )
                        elif skey == 'logic':
                            for m in cfg['startup']['logic']:
                                if 'module' in cfg['startup']['logic'][m]:
                                    self.tree['start']['logic'][m] = cfg['startup']['logic'][m]
                                else:
                                    self.logger.print_logMsg("    --> Ignoring startup GUI {0} -- no module specified".format(m) )

                # global config
                elif key == 'global':
                    for m in cfg['global']:
                        if m == 'storageDir':
                            self.logger.print_logMsg("=== Setting base directory: {0} ===".format(m) + cfg['global']['storageDir'])
                            self.setBaseDir(cfg['global']['storageDir'])
                
                        elif m == 'useOpenGL':
                            # use accelerated drawing
                            pg.setConfigOption('useOpenGL', cfg['global']['useOpenGl'])

                        elif m == 'stylesheet':
                            stylesheetpath = os.path.join(self.getMainDir(), 'artwork', 'styles', 'application', cfg['global']['stylesheet'])
                            if not os.path.isfile(stylesheetpath):
                                self.logger.print_logMsg("Stylesheet not found at {0}".format(stylesheetpath), importance=6, msgType='warning')
                                continue
                            stylesheetfile = open(stylesheetpath)
                            stylesheet = stylesheetfile.read()
                            stylesheetfile.close()
                            QtGui.QApplication.instance().setStyleSheet(stylesheet)
                            testwidget = QtGui.QWidget()
                            testwidget.ensurePolished()
                            bgcolor = testwidget.palette().color(QtGui.QPalette.Normal, testwidget.backgroundRole())
                            # set manually the background color in hex code according to our color scheme: 
                            pg.setConfigOption('background', bgcolor)
                
                # Copy in any other configurations.
                # dicts are extended, all others are overwritten.
                else:
                    if isinstance(cfg[key], dict):
                        if key not in self.tree['config']:
                            self.tree['config'][key] = {}
                        for key2 in cfg[key]:
                            self.tree['config'][key][key2] = cfg[key][key2]
                    else:
                        self.tree['config'][key] = cfg[key]
            except:
                printExc("Error in configuration:")
        # print self.tree['config']
        self.sigConfigChanged.emit()

    def listConfigurations(self):
        """Return a list of the available named configurations.

          @return list: user configurations
        """
        with self.lock:
            if 'configurations' in self.tree['config']:
                return list(self.tree['config']['configurations'].keys())
            else:
                return []

    def loadDefinedConfig(self, name):
        """ Loads the specified configuration file encoded in name parameter.
        
          @param string name: Name of the loadable configuration file.
        """
        with self.lock:
            if name not in self.tree['config']['configurations']:
                raise Exception('Could not find configuration named {0}'.format(name) )
            cfg = self.tree['config']['configurations'].get(name, )
        self.configure(cfg)

    def readConfigFile(self, fileName, missingOk=True):
        """Actually check if the configuration file exists and read it

          @param string fileName: path to configuration file
          @param bool missingOk: suppress exception if file does not exist

          @return dict: configuration from file
        """
        with self.lock:
            if os.path.isfile(fileName):
                return configfile.readConfigFile(fileName)
            else:
                fileName = self.configFileName(fileName)
                if os.path.isfile(fileName):
                    return configfile.readConfigFile(fileName)
                else:
                    if missingOk: 
                        return {}
                    else:
                        raise Exception('Config file {0} not found.'.format(fileName) )
            
    def writeConfigFile(self, data, fileName):
        """Write a file into the currently used config directory.

          @param dict data: dictionary to write into file
          @param string fileName: path for filr to be written
        """
        with self.lock:
            fileName = self.configFileName(fileName)
            dirName = os.path.dirname(fileName)
            if not os.path.exists(dirName):
                os.makedirs(dirName)
            configfile.writeConfigFile(data, fileName)
    
    def appendConfigFile(self, data, fileName):
        """Append configuration to a file in the currently used config directory.

          @param dict data: dictionary to write into file
          @param string fileName: path for filr to be written
        """
        with self.lock:
            fileName = self.configFileName(fileName)
            if os.path.exists(fileName):
                configfile.appendConfigFile(data, fileName)
            else:
                raise Exception("Could not find file {0}".format(fileName) )
        
        
    def configFileName(self, name):
        """Get the full path of a configuration file from its filename.

          @param string name: filename of file in configuration directory
          
          @return string: full path to file
        """
        with self.lock:
            return os.path.join(self.configDir, name)

    def saveConfig(self, filename):
        self.writeConfigFile(self.tree['defined'], filename)
        self.logger.logMsg('Saved configuration to {0}'.format(filename), msgType='status')

    def loadConfig(self, filename):
        newconfig = self.readConfigFile(filename)
        self.logger.logMsg('Loaded configuration from {0}'.format(filename), msgType='status')

    def setBaseDir(self, path):
        """Set base directory for data

          @param string path: base directory path
        """
        oldBaseDir = self.baseDir
        dirName = os.path.dirName(path)
        
        if not os.path.exists(dirName):
            os.makedirs(dirName)
            
        self.baseDir = dirName
        if(oldBaseDir != dirName):
            # emit a signal to the created Qt signal slot:
            self.sigBaseDirChanged.emit()

    ##################
    # Module loading #
    ##################

    def importModule(self, baseName, module):
        """Load a python module that is a loadable QuDi module.

          @param string baseName: the module base package (hardware, logic, or gui)
          @param string module: the python module name inside the base package

          @return object: the loaded python module
        """
        
        self.logger.print_logMsg('Loading module ".{0}.{1}"'.format(baseName, module) )
        if baseName not in ['hardware', 'logic', 'gui']:
            raise Exception('You are trying to cheat the '
                            'system with some category {0}'.format(baseName) )
        
        # load the python module
        mod = importlib.__import__('{0}.{1}'.format(baseName, module), fromlist=['*'])
        return mod
 
    def configureModule(self, moduleObject, baseName, className, instanceName, 
                        configuration = {} ):
        """Instantiate an object from the class that makes up a QuDi module
           from a loaded python module object.

          @param object moduleObject: loaded python module
          @param string baseName: module base package (hardware, logic or gui)
          @param string className: name of the class we want an object from 
                                 (same as module name usually)
          @param string instanceName: unique name thet the QuDi module instance
                                 was given in the configuration
          @param dict configuration: configuration options for the QuDi module

          @return object: QuDi module instance (object of the class derived
                          from Base)

          This method will add the resulting QuDi module instance to internal
          bookkeeping.
        """
        self.logger.print_logMsg('Configuring {0} as {1}'.format(className, instanceName) )
        with self.lock:
            if baseName in ['hardware', 'logic', 'gui']:
                if instanceName in self.tree['loaded'][baseName]:
                    raise Exception('{0} already exists with '
                                    'name {1}'.format(baseName, instanceName))
            else:
                raise Exception('You are trying to cheat the system with some '
                                'category {0}'.format(baseName) )
        
        if configuration is None:
            configuration = {}

        # get class from module by name
        modclass = getattr(moduleObject, className)
        
        #FIXME: Check if the class we just obtained has the right inheritance
        if not issubclass(modclass, Base):
            raise Exception('Bad inheritance, for instance %s from %s.%s.' % (instanceName, baseName, className))

        # Create object from class (Manager, Name, config)
        instance = modclass(self, instanceName, configuration)

        # Connect to log
        instance.sigLogMessage.connect(self.logger.queuedLogMsg)

        with self.lock:
            if baseName in ['hardware', 'logic', 'gui']:
                self.tree['loaded'][baseName][instanceName] = instance
            else:
                raise Exception('We checked this already, there is no way that '
                                'we should get base class {0} here'.format(baseName))
        
        self.sigModulesChanged.emit()
        return instance

    def connectModule(self, base, mkey):
        """ Connects the given module in mkey to main object with the help of base.
        
          @param string base: module base package (hardware, logic or gui)  
          @param string mkey: module which you want to connect
        
        """
        thismodule = self.tree['defined'][base][mkey]
        if mkey not in self.tree['loaded'][base]:
            self.logger.logMsg('Loading of {0} module {1} as {2} was not  '
                               'successful, not connecting it.'.format(base, thismodule['module'], mkey),
                               msgType='error')
            return
        if 'connect' not in thismodule:
            return
        if 'in' not in  self.tree['loaded'][base][mkey].connector:
            self.logger.logMsg('{0} module {1} loaded as {2} is supposed to '
                               'get connected but it does not declare any IN '
                               'connectors.'.format(base, thismodule['module'], mkey),
                               msgType='error')
            return
        if 'module' not in thismodule:
            self.logger.logMsg('{0} module {1} ({2}) connection configuration '
                               'is broken: no module defined.'.format(base, mkey, thismodule['module'] ),
                               msgType='error')
            return
        if not isinstance(thismodule['connect'], OrderedDict):
            self.logger.logMsg('{0} module {1} ({2}) connection configuration '
                               'is broken: connect is not a dict.'.format(base, mkey, thismodule['module'] ),
                               msgType='error')
            return

        connections = thismodule['connect']
        for c in connections:
            connectorIn = self.tree['loaded'][base][mkey].connector['in']
            if c not in connectorIn:
                self.logger.logMsg('IN connector {0} of {1} module {2} loaded '
                                   'as {3} is supposed to get connected but '
                                   'is not declared in the module.'.format(c, base, thismodule['module'], mkey),
                                   msgType='error')
                continue
            if not isinstance(connectorIn[c], OrderedDict):
                self.logger.logMsg('No dict.', msgType='error')
                continue
            if 'class' not in connectorIn[c]:
                self.logger.logMsg('no class key in connection declaration',
                                   msgType='error')
                continue
            if not isinstance(connectorIn[c]['class'], str):
                self.logger.logMsg('value for class key is not a string',
                                   msgType='error')
                continue
            if 'object' not in connectorIn[c]:
                self.logger.logMsg('no object key in connection declaration',
                                   msgType='error')
                continue
            if connectorIn[c]['object'] is not None:
                self.logger.logMsg('IN connector {0} of {1} module {2} loaded as {3} is already connected.'.format(c, base, thismodule['module'], mkey), msgType='warning')
                continue
            if not isinstance(connections[c], str):
                self.logger.logMsg('{0} module {1} ({2}) connection '
                                   'configuration is broken, value for key '
                                   '{3 }is not a string.'.format(base, mkey, thismodule['module'], c ),
                                   msgType='error')
                continue
            if '.' not in connections[c]:
                self.logger.logMsg('{0} module {1} ({2}) connection '
                                   'configuration is broken, value {3} for '
                                   'key {4} does not contain a dot.'.format(base, mkey, thismodule['module'], connections[c], c ),
                                   msgType='error')
                continue
            destmod = connections[c].split('.')[0]
            destcon = connections[c].split('.')[1]
            destbase = ''
            if destmod in self.tree['loaded']['hardware'] and destmod in self.tree['loaded']['logic']:
                self.logger.logMsg('Unique name {0} is in both hardware and '
                                   'logic module list. Connection is not well '
                                   'defined, cannot connect {1} ({2}) to  it.'.format(destmod, mkey, thismodule['module']),
                                   msgType='error')
                continue
                
            # Connect to hardware module
            elif destmod in self.tree['loaded']['hardware']:
                destbase = 'hardware'
            elif destmod in self.tree['loaded']['logic']:
                destbase = 'logic'
            else:
                self.logger.logMsg('Unique name {0} is neither in hardware or '
                                   'logic module list. Cannot connect {1} ({2}) '
                                   'to it.'.format(connections[c], mkey, thismodule['module']),
                                   msgType='error')
                continue

            if 'out' not in self.tree['loaded'][destbase][destmod].connector:
                self.logger.logMsg('Module {0} loaded as {1} is supposed to '
                                   'get connected to module loaded as {2} but '
                                   'that does not declare any OUT '
                                   'connectors.'.format(thismodule['module'], mkey, destmod),
                                   msgType='error')
                continue
            outputs = self.tree['loaded'][destbase][destmod].connector['out']
            if destcon not in outputs:
                self.logger.logMsg('OUT connector not declared', 
                                   msgType='error')
                continue
            if not isinstance(outputs[destcon], OrderedDict):
                self.logger.logMsg('not a dict', msgType='error')
                continue
            if 'class' not in outputs[destcon]:
                self.logger.logMsg(
                    'no class key in dict',
                    msgType='error')
                continue
            if not isinstance(outputs[destcon]['class'], str):
                self.logger.logMsg('class value no string', msgType='error')
                continue
#            if not issubclass(self.tree['loaded'][destbase][destmod].__class__, outputs[destcon]['class']):
#                self.logger.logMsg('not the correct class for declared '
#                                   'interface', msgType='error')
#                return

            # Finally set the connection object
            self.logger.logMsg('Connecting {0} module {1}.IN.{2} to {3} '
                               '{4}.{5}'.format(base, mkey, c, destbase, destmod, destcon),
                               msgType='status')
            connectorIn[c]['object'] = self.tree['loaded'][destbase][destmod]

    def loadConfigureModule(self, base, key):
        """Loads the configuration Module in key with the help of base class.
          
          @param string base: module base package (hardware, logic or gui)  
          @param string key: module which is going to be loaded
          
        """
        if 'module' in self.tree['defined'][base][key]:
            if 'remote' in self.tree['defined'][base][key]:
                if not isinstance(self.tree['defined'][base][key]['remote'], str):
                    self.logger.logMsg('Remote URI of {0} module {1} not a string.'.format(base, key), msgType='error')
                    return
                try:
                    instance = self.rm.getRemoteModuleUrl(self.tree['defined'][base][key]['remote'])
                    self.logger.logMsg('Remote module {0} loaded as .{1}.{2}.'.format(self.tree['defined'][base][key]['remote'], base, key), msgType='status')
                    with self.lock:
                        if base in ['hardware', 'logic', 'gui']:
                            self.tree['loaded'][base][key] = instance
                        else:
                            raise Exception('You are trying to cheat the '
                                'system with some category {0}'.format(base) )
                except:
                    self.logger.logExc('Error while loading {0} module: {1}'.format(base, key), msgType='error')
            else:
                try:
                    modObj = self.importModule(base, self.tree['defined'][base][key]['module'])
                    pkgName = re.escape(modObj.__package__)
                    modName = re.sub('^{0}\.'.format(pkgName), '', modObj.__name__)
                    self.configureModule(modObj, base, modName, key, self.tree['defined'][base][key])
                    ## start main loop for qt objects
                    if base == 'logic':
                        modthread = self.tm.newThread('mod-' + base + '-' + key)
                        self.tree['loaded'][base][key].moveToThread(modthread)
                        modthread.start()
                    if 'remoteaccess' in self.tree['defined'][base][key] and self.tree['defined'][base][key]['remoteaccess']:
                        self.rm.shareModule(key, self.tree['loaded'][base][key])
                except:
                    self.logger.logExc('Error while loading {0} module: {1}'.format(base, key), msgType='error')
                    return
        else:
            self.logger.logMsg('Missing module declaration in configuration: {0}.{1}'.format(base, key), msgType='error')

    def reloadConfigureModule(self, base, key):
        """Reloads the configuration module in key with the help of base class.
          
          @param string base: module base package (hardware, logic or gui)
          @param string key: module which is going to be loaded
          
        """
        if key in self.tree['loaded'][base] and 'module' in self.tree['defined'][base][key]:
            try:
                # state machine: deactivate
                self.tree['loaded'][base][key].deactivate()
                # stop main loop for qt objects
                if base == 'logic':
                    self.tm.quitThread('mod-' + base + '-' + key)
            except:
                self.logger.logExc('Error while deactivating {0} module: {1}'.format(base, key), msgType='error')
                return
            try:
                with self.lock:
                    self.tree['loaded'][base].pop(key, None)
                modObj = self.importModule(base, self.tree['defined'][base][key]['module'])
                # des Pudels Kern
                importlib.reload(modObj)
                pkgName = re.escape(modObj.__package__)
                modName = re.sub('^{0}\.'.format(pkgName), '', modObj.__name__)
                self.configureModule(modObj, base, modName, key, self.tree['defined'][base][key])
                # start main loop for qt objects
                if base == 'logic':
                    modthread = self.tm.newThread('mod-' + base + '-' + key)
                    self.tree['loaded'][base][key].moveToThread(modthread)
                    modthread.start()
            except:
                self.logger.logExc('Error while reloading {0} module: {1}'.format(base, key), msgType='error')
                return
        else:
            self.logger.logMsg('Module not loaded or not loadable (missing module declaration in configuration): {0}.{1}'.format(base, key), msgType='error')

    def activateModule(self, base, key):
        """Activated the module given in key with the help of base class.
          
          @param string base: module base package (hardware, logic or gui)  
          @param string key: module which is going to be activated.
            
        """
        if self.tree['loaded'][base][key].getState() != 'deactivated' and (
                ( base in self.tree['defined'] and key in self.tree['defined'][base]  and 'remote' in self.tree['defined'][base][key])
                or (base in self.tree['start'] and  key in self.tree['start'][base])) :
            return
        if self.tree['loaded'][base][key].getState() != 'deactivated':
            self.logger.logMsg('{0} module {1} not deactivated anymore'.format(base, key),
                               msgType='error')
            return
        try:
            self.tree['loaded'][base][key].activate()
        except:
            self.logger.logExc('{0} module {1}: error during activation:'.format(base, key),
                               msgType='error')

    def getSimpleModuleDependencies(self, base, key):
        """ Based on object id, find which connections to replace.

          @param str base: Module category
          @param str key: Unique configured module name for module where we want the dependencies

          @return dict: module dependencies in the right format for the Manager.toposort function
        """
        deplist = list()
        if base not in self.tree['loaded']:
            self.logger.logMsg('{0} module {1}: no such base'.format(base, key), msgType='error')
            return None
        if key not in self.tree['loaded'][base]:
            self.logger.logMsg('{0} module {1}: no such module defined'.format(base, key), msgType='error')            
            return None
        for mbase in self.tree['loaded']:
            for mkey in self.tree['loaded'][mbase]:
                target = self.tree['loaded'][mbase][mkey]
                if not hasattr(target, 'connector'):
                    self.logger.logMsg('No connector in module .{0}.{1}!'.format(mbase, mkey), msgType='error')
                    continue
                for conn in target.connector['in']:
                    if not 'object' in target.connector['in'][conn]:
                        self.logger.logMsg('Malformed connector {2} in module .{0}.{1}!'.format(mbase, mkey, conn), msgType='error')
                        continue
                    if target.connector['in'][conn]['object'] is self.tree['loaded'][base][key]:
                        deplist.append( (mbase, mkey) )
        return {key: deplist}


    def getRecursiveModuleDependencies(self, base, key):
        """ Based on input connector declarations, determine in which other modules are needed for a specific module to run.

          @param str base: Module category
          @param str key: Unique configured module name for module where we want the dependencies

          @return dict: module dependencies in the right format for the Manager.toposort function
        """
        deps = dict()
        if base not in self.tree['defined']:
            self.logger.logMsg('{0} module {1}: no such base'.format(base, key), msgType='error')
            return None
        if key not in self.tree['defined'][base]:
            self.logger.logMsg('{0} module {1}: no such module defined'.format(base, key), msgType='error')            
            return None
        if 'connect' not in self.tree['defined'][base][key]:
            return dict()
        if not isinstance(self.tree['defined'][base][key]['connect'], OrderedDict):
            self.logger.logMsg('{0} module {1}: connect is not a dictionary'.format(base, key), msgType='error')            
            return None
        connections = self.tree['defined'][base][key]['connect']
        deplist = list()
        for c in connections:
            if not isinstance(connections[c], str):
                self.logger.logMsg('value for class key is not a string', msgType='error')
                return None
            if not '.' in connections[c]:
                self.logger.logMsg('wrong format for connection target: {0}'.format(connections[c]), msgType='error')
                return None
            destmod = connections[c].split('.')[0]
            destbase = ''
            if destmod in self.tree['defined']['hardware'] and destmod in self.tree['defined']['logic']:
                self.logger.logMsg('Unique name {0} is in both hardware and '
                                   'logic module list. Connection is not well '
                                   'defined.'.format(destmod),
                                   msgType='error')
                return None
            elif destmod in self.tree['defined']['hardware']:
                destbase = 'hardware'
            elif destmod in self.tree['defined']['logic']:
                destbase = 'logic'
            else:
                self.logger.logMsg('Unique name {0} is neither in hardware or '
                                   'logic module list. Cannot connect {1}  '
                                   'to it.'.format(connections[c], key),
                                   msgType='error')
                return None
            deplist.append(destmod)
            subdeps = self.getRecursiveModuleDependencies(destbase, destmod)
            if subdeps is not None:
                deps.update(subdeps)
            else:
                return None
        if len(deplist) > 0:
            deps.update({key: deplist})
        return deps

    @QtCore.pyqtSlot(str, str)
    def startModule(self, base, key):
        """ Figure out the module dependencies in terms of connections, load and activate module.

          @param str base: Module category
          @param str key: Unique module name

            If the module is already loaded, just activate it.
            If the module is an active GUI module, show its window.
        """
        deps = self.getRecursiveModuleDependencies(base, key)
        sorteddeps = Manager.toposort(deps)
        if len(sorteddeps) == 0:
            sorteddeps.append(key)

        for mkey in sorteddeps:
            for mbase in ['hardware', 'logic', 'gui']:
                if mkey in self.tree['defined'][mbase] and not mkey in self.tree['loaded'][mbase]:
                    self.loadConfigureModule(mbase, mkey)
                    self.connectModule(mbase, mkey)
                    if mkey in self.tree['loaded'][mbase]:
                        self.activateModule(mbase, mkey)
                elif mkey in self.tree['defined'][mbase] and mkey in self.tree['loaded'][mbase]:
                    if self.tree['loaded'][mbase][mkey].getState() == 'deactivated':
                        self.tree['loaded'][mbase][mkey].activate()
                    elif self.tree['loaded'][mbase][mkey].getState() != 'deactivated' and mbase == 'gui':
                        self.tree['loaded'][mbase][mkey].show()

    @QtCore.pyqtSlot(str, str)
    def stopModule(self, base, key):
        """Deactivate Module.
          @param str base: Module category
          @param str key: Unique module name
        """
        for mbase in ['hardware', 'logic', 'gui']:
            if key in self.tree['loaded'][mbase] and self.tree['loaded'][mbase][key].getState() in ['idle', 'running']:
                self.tree['loaded'][mbase][key].deactivate()
            

    @QtCore.pyqtSlot(str, str)
    def restartModuleSimple(self, base, key):
        """Deactivate, reloade, activate module.
          @param str base: Module category
          @param str key: Unique module name

            Deactivates and activates all modules that depend on it in order to ensure correct connections.
        """
        deps = self.getSimpleModuleDependencies(base, key)
        if deps is None:
            return
        # Remove references
        for depmod in deps[key]:
            destbase, destmod = depmod
            for c in self.tree['loaded'][destbase][destmod].connector['in']:
                if self.tree['loaded'][destbase][destmod].connector['in'][c]['object'] is self.tree['loaded'][base][key]:
                    self.tree['loaded'][destbase][destmod].deactivate()
                    self.tree['loaded'][destbase][destmod].connector['in'][c]['object'] = None

        self.reloadConfigureModule(base, key)
        self.connectModule(base, key)
        self.activateModule(base,key)

        for depmod in deps[key]:
            destbase, destmod = depmod
            self.connectModule(destbase, destmod)
            self.activateModule(destbase, destmod)

    @QtCore.pyqtSlot(str, str)
    def restartModuleRecursive(self, base, key):
        """ Figure out the module dependencies in terms of connections, reload and activate module.

          @param str base: Module category
          @param str key: Unique configured module name

        """
        deps = self.getSimpleModuleDependencies(base, key)
        sorteddeps = Manager.toposort(deps)

        for mkey in sorteddeps:
            for mbase in ['hardware', 'logic', 'gui']:
                # load if the config changed
                if mkey in self.tree['defined'][mbase] and not mkey in self.tree['loaded'][mbase]:
                    self.loadConfigureModule(mbase, mkey)
                    self.connectModule(mbase, mkey)
                    if mkey in self.tree['loaded'][mbase]:
                        self.activateModule(mbase, mkey)
                # reload if already there
                elif mkey in self.tree['loaded'][mbase]:
                    self.reloadConfigureModule(mbase, mkey)
                    self.connectModule(mbase, mkey)
                    if mkey in self.tree['loaded'][mbase]:
                        self.activateModule(mbase, mkey)

    def startAllConfiguredModules(self):
        """Connect all QuDi modules from the currently laoded configuration and
            activate them.
        """
        #FIXME: actually load all the modules in the correct order and connect
        # the interfaces
        for base in ['hardware', 'logic', 'gui']:
            for key in self.tree['defined'][base]:
                self.startModule(base, key)

        self.logger.print_logMsg('Activation finished.')

    def quit(self):
        """Nicely request that all modules shut down."""
        for mbase in ['hardware', 'logic', 'gui']:
            for module in self.tree['loaded'][mbase]:
                self.stopModule(mbase, module)
                QtCore.QCoreApplication.processEvents()
        self.sigManagerQuit.emit(self)

    # Staticmethods are used to group functions which have some logical 
    # connection with a class but they They behave like plain functions except 
    # that you can call them from an instance or the class. Methods covered 
    # with static decorators are an organization/stylistic feature in python.
    @staticmethod
    def toposort(deps, cost=None):
        """Topological sort. Arguments are:
        
          @param dict deps: Dictionary describing dependencies where a:[b,c]
                            means "a depends on b and c"
          @param dict cost: Optional dictionary of per-node cost values. This
                            will be used to sort independent graph branches by 
                            total cost. 
                
        Examples::

            # Sort the following graph:
            # 
            #   B ──┬─────> C <── D
            #       │       │       
            #   E <─┴─> A <─┘
            #     
            deps = {'a': ['b', 'c'], 'c': ['b', 'd'], 'e': ['b']}
            toposort(deps)
            => ['b', 'e', 'd', 'c', 'a']
            
            # This example is underspecified; there are several orders that 
            # correctly satisfy the graph. However, we may use the 'cost' 
            # argument to impose more constraints on the sort order.
            
            # Let each node have the following cost:
            cost = {'a': 0, 'b': 0, 'c': 1, 'e': 1, 'd': 3}
            
            # Then the total cost of following any node is its own cost plus
            # the cost of all nodes that follow it:
            #   A = cost[a]
            #   B = cost[b] + cost[c] + cost[e] + cost[a]
            #   C = cost[c] + cost[a]
            #   D = cost[d] + cost[c] + cost[a]
            #   E = cost[e]
            # If we sort independent branches such that the highest cost comes 
            # first, the output is:
            toposort(deps, cost=cost)
            => ['d', 'b', 'c', 'e', 'a']
        """
        # copy deps and make sure all nodes have a key in deps
        deps0 = deps
        deps = {}
        for k,v in list(deps0.items()):
            deps[k] = v[:]
            for k2 in v:
                if k2 not in deps:
                    deps[k2] = []

        # Compute total branch cost for each node
        key = None
        if cost is not None:
            order = Manager.toposort(deps)
            allDeps = {n: set(n) for n in order}
            for n in order[::-1]:
                for n2 in deps.get(n, []):
                    allDeps[n2] |= allDeps.get(n, set())
                    
            totalCost = {n: sum([cost.get(x, 0) for x in allDeps[n]]) for n in allDeps}
            key = lambda x: totalCost.get(x, 0)

        # compute weighted order
        order = []
        while len(deps) > 0:
            # find all nodes with no remaining dependencies
            ready = [k for k in deps if len(deps[k]) == 0]
            
            # If no nodes are ready, then there must be a cycle in the graph
            if len(ready) == 0:
                print(deps)
                raise Exception('Cannot resolve requested device '
                                'configure/start order.')
            
            # sort by branch cost
            if key is not None:
                ready.sort(key=key, reverse=True)
            
            # add the highest-cost node to the order, then remove it from the
            # entire set of dependencies
            order.append(ready[0])
            del deps[ready[0]]
            for v in list(deps.values()):
                try:
                    v.remove(ready[0])
                except ValueError:
                    pass
        
        return order
        
