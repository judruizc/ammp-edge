import logging
logger = logging.getLogger(__name__)

import yaml, json
import requests
import sys, os
import time

from db_model import NodeConfig
from .events import NodeEvents
from .config_watch import ConfigWatch


class Node(object):

    def __init__(self):

        try:
            # Load base config from YAML file
            with open(os.path.join(os.getenv('SNAP', './'), 'remote.yml'), 'r') as remote_yml:
                self.remote = yaml.load(remote_yml)
        except:
            logger.exception('Base configuration file remote.yml cannot be loaded. Quitting')
            sys.exit('Base configuration file remote.yml cannot be loaded. Quitting')

        try:
            self._dbconfig = NodeConfig.get()
        except NodeConfig.DoesNotExist:
            logger.info('No node configuration found in internal database. Attempting node initialization')
            self.__initialize()

        self.node_id = self._dbconfig.node_id
        self.access_key = self._dbconfig.access_key

        logger.info('Node ID: %s', self.node_id)

        self.events = NodeEvents()
        config_watch = ConfigWatch(self)
        config_watch.start()

        if self._dbconfig.config:
            # Configuration is available in DB; use this
            logger.info('Using stored configuration from database')
            self.config = self._dbconfig.config

            # Check for a new configuration anyway
            self.events.check_new_config.set()
        else:
            # Need to request configuration from API
            logger.info('No stored configuration in database, or configuration reset requested')

            self.config = None

            # Request a new configuration from the other thread, and wait for it to be obtained
            self.events.check_new_config.set()
            with self.events.getting_config:
                self.events.getting_config.wait_for(lambda: self.config is not None)

            self._dbconfig.config = self.config
            self._dbconfig.save()

        self.drivers = self.__get_drivers()



    @property
    def node_id(self):
        return self._node_id

    @node_id.setter
    def node_id(self, value):
        self._node_id = value

    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, value):
        self._config = value        

    @property
    def access_key(self):
        return self._access_key

    @access_key.setter
    def access_key(self, value):
        self._access_key = value

    @property
    def drivers(self):
        return self._drivers

    @drivers.setter
    def drivers(self, value):
        self._drivers = value        


    def __initialize(self):
        node_id = self.__generate_node_id()
        logger.info('Generated node ID %s' % node_id)

        access_key = self.__do_node_activation(node_id)
        if not access_key:
            logger.error('Unable to obtain access key')
            return None
            #### TODO: This needs to be set to keep retrying if it fails! Otherwise we just throw an exception

        # Save node_id and access_key in database
        self._dbconfig = NodeConfig.create(node_id=node_id, access_key=access_key)
        self._dbconfig.save()


    def __generate_node_id(self):
        # Get ID (ideally hardware MAC address) that is used to identify logger when pushing data

        # First try to get the address of the primary Ethernet adapter
        try:
            import netifaces as nif

            ifn_wanted = ['eth0', 'en0', 'eth1', 'en1', 'em0', 'em1', 'wlan0', 'wlan1']
            ifn_available = nif.interfaces()

            ifn = [i for i in ifn_wanted if i in ifn_available][0]

            if_mac = nif.ifaddresses(ifn)[nif.AF_LINK][0]['addr']
            node_id = if_mac.replace(':','')

        except Exception as ex:
            logger.exception('Cannot find primary network interface MAC; trying UUID MAC')

            # If that doesn't work, try doing it via the UUID method
            try:
                from uuid import getnode
                
                uuid_node = getnode()
                node_id = "{0:0{1}x}".format(uuid_node, 12)

            except Exception as ex:
                logger.exception('Cannot get MAC via UUID method; generating random node ID')

                # If that also doesn't work, generate a random 12-character hex string
                import random
                node_id = '%012x' % random.randrange(16**12)

        return node_id

    def __do_node_activation(self, node_id):

        # Initiate activation
        logger.info('Requesting activation for node %s' % node_id)
        
        try:
            r1 = requests.get('https://%s/api/%s/nodes/%s/activate' % (self.remote['host'], self.remote['apiver'], node_id))
            rtn = json.loads(r1.text)

            if r1.status_code == 200:
                access_key = rtn['access_key']
                logger.info('Obtained API key')
                if rtn:
                    logger.debug('API response: %s' % rtn)
            else:
                logger.error('Error %d requesting activation from API' % r1.status_code)
                if rtn:
                    logger.debug('API response: %s' % rtn)
                return None
        except Exception as ex:
            logger.exception('Exception raised while requesting activation from API')
            return None

        # Confirm activation
        logger.info('Confirming activation for node %s' % node_id)

        try:
            r2 = requests.post('https://%s/api/%s/nodes/%s/activate' % (self.remote['host'], self.remote['apiver'], node_id),
                headers={'Authorization': access_key})
            rtn = json.loads(r2.text)

            if r2.status_code == 200:
                logger.info('Confirmed activation')
                if rtn:
                    logger.debug('API response: %s' % rtn)
            else:
                logger.error('Error %d confirming activation with API' % r2.status_code)
                if rtn:
                    logger.debug('API response: %s' % rtn)
                return None
        except Exception as ex:
            logger.exception('Exception raised while confirming activation with API')
            return None

        return access_key
    

    def __get_drivers(self):

        drivers = {}

        drvpath = os.path.join(os.getenv('SNAP', './'), 'drivers')

        driver_files = [pos_json for pos_json in os.listdir(drvpath) if pos_json.endswith('.json')]
        for drv in driver_files:
            with open(os.path.join(drvpath, drv)) as driver_file:
                drivers[os.path.splitext(drv)[0]] = json.load(driver_file)
                logger.info('Loaded driver %s' % drv)

        return drivers

