import paho.mqtt.client as mqtt
import paho.mqtt.publish as publisher
import time
import json
import numpy as np
import sys
import copy
import pymongo
import pymongo.collection
import signal

from flask import Flask, abort, Response, request
from threading import Thread, Lock

from db_interface import DBInterface
from triangulator import Triangulate

#######################################   GLOBAL VARIABLES   #######################################

# beacon dictionary <name, BeaconInfo>
beaconTable = {}

# Content of configuration file
configFileContent = {}

# Mutex for beaconTable
beaconTableLocker = None

# Mutex for configFileContent
configFCLocker = None

# Configuration file name
configFileName = ""

# Topic's name on which publish updates for sniffers
pubTopic= ""

# Instance of web-server
webApp = Flask(__name__)



"""
	Instance of the database
"""
database = None


#######################################   HELPER FUNCTIONS   #######################################


# Function to save configFileContent to to the file
def  storeConfigurationFile () :
	print ("Saving configuration to " + configFileName)
	f= open(configFileName, 'w')
	json.dump(configFileContent, f, indent=4)
	f.close()



# Function to convert rooms list to an array
def roomsToArray () :
	
	arr= []
	for p in configFileContent["positions"] :
		arr.append (configFileContent["positions"][p])
	return arr



# Function to translate room name to room id
def roomNameToId (rn) :
	rid= ""
	if rn != "" :
		for p in configFileContent["positions"] :
			if (configFileContent["positions"][p] == rn) :
				rid = p
				break
	return rid



# Function to check if a string represents a mac address
def isMacAddress (ma) :
	# TODO
	return True


#######################################   CLASSES   #######################################

class BeaconInfo():
	"""
		Class for a beacon

		Instances:
			__map: 	a dictionary map <room, measure_list>
			__id:	Beacon's id
			__last:	last room a person was detected
	"""


	def __init__(self, id, sids):
		self.__map = dict()
		self.__id = id
		self.__last = ""


	# Getters
	def getMap(self):
		return self.__map


	def getId(self):
		return self.__id


	def getLast(self):
		return self.__last


	# Setters
	def setLast(self, last):
		self.__last = last


	# Other Methods
	def addMeasure(self, sid, measure):
		"""
			Add a received measure from a given room
		"""

		if not self.__map.has_key(str(sid)):
			self.__map[str(sid)] = []

		self.__map[str(sid)].extend(measure)


	def cleanInfo(self):
		"""
			Clean the current (rssi) values from the map
		"""
		for e in self.__map:
			self.__map[e] = []


class WebServer(Thread):
    """
        Thread running the web server
    """

    def __init__(self, app, ip, port):
		Thread.__init__(self)
		self.__flaskApp= app
		self.__port= port
		self.__ip = ip


    def run(self):
		self.__flaskApp.run(self.__ip, self.__port)


#######################################   REST INTERFACE   #######################################

## Routing rules for web server thread
@webApp.route('/')
def root():
    return Response("Welcome to REST API!", status=200, content_type="text/plain")

#####
#####     /rooms
@webApp.route("/rooms", methods=["GET"])
def roomsGet():
	return Response(json.dumps(roomsToArray()), status=200, content_type="application/json")

#####
#####     /rooms/<rn>
@webApp.route("/rooms/<rn>", methods=['GET'])
def getRooms (rn):
	rooms= roomsToArray()
	rid = roomNameToId(rn)

	if rn == "" : 
		return Response("Room is empty", status=400, content_type="text/plain")
	elif not rn in rooms :
		return Response("Requested room doesn't exists", status=400, content_type="text/plain")
	else :
		ls = []
		beaconTableLocker.acquire (True)
		# Searching all people in a required room
		for b in beaconTable :
			if beaconTable[b].getLast() == rid :
				ls.append(beaconTable[b].getId())
		beaconTableLocker.release()
		return Response (json.dumps(ls), status=200, content_type="application/json")



@webApp.route("/rooms/<rn>", methods=['POST'])
def postRooms(rn):
	toRet= None
	configFCLocker.acquire(True)

	rooms= configFileContent["positions"].values()

	if rn == "" :
		toRet= Response("Room is empty!", status=400, content_type="text/plain")
	elif request.data == "" :
		toRet = Response("Invalid raspberry mac", status=400, content_type="text/plain")
	elif rn in rooms :
		toRet= Response("Requested room already exists!", status=400, content_type="text/plain")
	else :
		entry= {request.data : rn}
		print ("Creating room  " + str(entry))
		configFileContent["positions"][request.data]= rn
		storeConfigurationFile ()
		toRet= Response("", status=201, content_type="text/plain")
	configFCLocker.release()

	return toRet



@webApp.route("/rooms/<rn>", methods=['DELETE'])
def deleteRooms(rn):
	toRet= None
	configFCLocker.acquire(True)

	rooms= configFileContent["positions"].values()

	if rn == "" :
		toRet= Response("Room name is empty!", status=400, content_type="text/plain")
	elif not rn in rooms :
		toRet= Response("Room name  " + rn + "  doesn't exist!", status=400, content_type="text/plain")
	else :
		rid = roomNameToId (rn)

		beaconTableLocker.acquire(True)
		
		del configFileContent["positions"][rid]
		storeConfigurationFile ()
		
		for b in beaconTable :
			bo = beaconTable[b]
			if bo.getLast() == rid :
				bo.setLast("")
		beaconTableLocker.release ()
		database.delete_room_entries(rn)
		toRet= Response("", status=200, content_type="text/plain")
	configFCLocker.release()

	return toRet



#####
#####     /readings/<bid>
@webApp.route("/readings/<bid>", methods=['GET'])
def getReadings(bid):
	if bid == "" :
		return Response("Beacon id is empty!", status=400, content_type="text/plain")
	elif not beaconTable.has_key(bid) :
		return Response("Beacon id  " + bid + "  doesn't exist!", status=400, content_type="text/plain")
	else :
		beaconTableLocker.acquire(True)
		res= json.dumps(beaconTable[bid].getMap())
		beaconTableLocker.release ()
		return Response(res, status=200, content_type="application/javascript")


@webApp.route("/readings/<bid>", methods=['DELETE'])
def deleteReadings(bid):
	if bid == "" :
		return Response("Beacon id is empty!", status=400, content_type="text/plain")
	elif not beaconTable.has_key(bid) :
		return Response("Beacon id  " + bid + "  doesn't exist!", status=400, content_type="text/plain")
	else :
		beaconTableLocker.acquire(True)
		beaconTable[bid].cleanInfo()
		beaconTableLocker.release ()
		return Response("", status=200, content_type="text/plain")


#####
#####	/peopleList
@webApp.route("/peopleList", methods=["GET"])
def getPeopleList () :

	people= []
	configFCLocker.acquire (True)
	for d in configFileContent["devices"] :
		p= configFileContent["devices"][d]
		people.append(p)
	configFCLocker.release ()
	return Response (json.dumps(people))



#####
#####     /people
@webApp.route("/people", methods=["GET"])
def getPeopleLocations():

	people = dict()
	beaconTableLocker.acquire (True)
	for b in beaconTable:
		if not beaconTable[b].getLast() == "" :
			rid = beaconTable[b].getLast()
			people[b] = str(configFileContent["positions"][rid])
	beaconTableLocker.release()
	return Response(json.dumps(people), status=200, content_type="application/json")


#####
#####     /people/<rid>
@webApp.route("/people/<pid>", methods=["POST"])
def postPeople(pid):
	toRet= None
	configFCLocker.acquire(True)
	beaconTableLocker.acquire(True)
	if pid == "" :
		toRet= Response("Beacon id is empty!", status=400, content_type="text/plain")
	elif beaconTable.has_key (pid) :
		toRet= Response("Beacon with id  " + pid + "  already exists!", status=400, content_type="text/plain")
	elif request.data in configFileContent["devices"]:
		toRet = Response("Mac address  " + request.data + "  already in use!", status=400, content_type="text/plain")
	else :
		rs= roomsToArray()

		beaconTable[pid]= BeaconInfo(pid, rs)
		configFileContent["devices"].update({request.data:pid})
		storeConfigurationFile()
		
		# Publishing new person to all people with mac address
		payload = {
			"action" : "add",
			"mac" : request.data
		}
		brokerAddress = configFileContent["broker-ip"]
		publisher.single (pubTopic, json.dumps(payload), hostname=brokerAddress)

		toRet= Response('', status=201, content_type="text/plain")
	beaconTableLocker.release()
	configFCLocker.release()
	return toRet



@webApp.route("/people/<pid>", methods=["DELETE"])
def deletePeople(pid):
	toRet= None
	configFCLocker.acquire(True)
	beaconTableLocker.acquire(True)

	if pid == "" :
		toRet= Response("Beacon id is empty!", status=400, content_type="text/plain")
	elif not beaconTable.has_key (pid) :
		toRet= Response("Beacon with id  " + pid + "  doesn't exist!", status=400, content_type="text/plain")
	else :
		beaconTable.pop(pid)
		database.delete_device_entries(pid)
		mac_address = ""
		for mac, name in configFileContent["devices"].items():
			if name ==pid:
				mac_address = mac

		print("Deleting "+mac_address+" association with user "+pid)
		configFileContent["devices"].pop(mac_address)
		storeConfigurationFile()
		
		# Deleting person
		payload = {
			"action" : "delete",
			"mac" : mac_address,
		}
		brokerAddress = configFileContent["broker-ip"]
		publisher.single (pubTopic, json.dumps(payload), hostname=brokerAddress)

		toRet= Response('', status=200, content_type="application/json")
	beaconTableLocker.release()
	configFCLocker.release()
	return toRet


#######################################   MQTT CALLBACKS   #######################################

def on_message(client, userdata, message):
	"""
		Broker callback once a msg is received
	"""

	jsonMsg = json.loads(message.payload.decode("utf-8"))
	configFCLocker.acquire (True)
	beaconTableLocker.acquire(True)

	# Checking if the sender of message is registered
	found = False
	for p in configFileContent["positions"] :
		#if configFileContent["positions"][p] == jsonMsg["station-id"] :
		if p == jsonMsg["station-id"] :
			found= True

	if not found :
		print ("Station id  " + jsonMsg["station-id"] + "  doesn't correspond to any registered room!")
	
	else :
		for mac in jsonMsg["map"] :
			try:
				user = configFileContent["devices"][mac]
				if beaconTable.has_key(user) :
					beaconTable[user].addMeasure(jsonMsg["station-id"],  jsonMsg["map"][mac])
				
			except(KeyError):
				print("Utente " + mac + " rimosso")

	beaconTableLocker.release()
	configFCLocker.release ()

	"""
		forma del payload
		{
			"stid" : value
			{
				"mac_address1" : [rssi_1, ..., rssi_n]
				"mac_address2" : [rssi_1, ..., rssi_n]
				"mac_address3" : [rssi_1, ..., rssi_n]
				"mac_address4" : [rssi_1, ..., rssi_n]
			}
		}
	"""


#######################################   MAIN   #######################################

def main():
	"""
		Main
	"""

	if len(sys.argv) != 2 :
		sys.exit("Wrong number of arguments!\n\tExiting")

	global beaconTable
	global beaconTableLocker
	global database
	global configFileContent
	global configFCLocker
	global configFileName
	global pubTopic


	print ("Initializing server")
	configFileName= sys.argv[1]
	configFileContent = json.load(open(configFileName))
	#print(json.dumps(configFileContent))

	beaconTable = dict()
	beaconTableLocker = Lock()
	configFCLocker= Lock()

	tmpIds= []
	for p in configFileContent["positions"] :
		tmpIds.append (p)

	for b in configFileContent["devices"].values() :
		beaconTable[b]= BeaconInfo(b, tmpIds)
	
	# Instantiate Broker
	broker_address = configFileContent["broker-ip"]
	subTopic = configFileContent["subscribe_topic"]
	pubTopic = configFileContent["publish_topic"]

	print ("Init broker")

	client = mqtt.Client("P1")
	client.connect(broker_address)
	print ("Subscription to " + broker_address + " on topic " + subTopic)
	client.subscribe(subTopic)
	client.on_message=on_message
	client.loop_start()

	# Initiate connection with MongoDB
	database = DBInterface(configFileContent["DB_connection_params"])

	# Activate triangulator thread
	triangulate = Triangulate(int(configFileContent["algorithm-interval"]), beaconTable, beaconTableLocker, database)
	triangulate.daemon = True
	triangulate.start()

    # Activating web-server thread
	webServer= WebServer(webApp, configFileContent["server-ip"], int(configFileContent["server-port"]))
	webServer.daemon = True
	webServer.start()

	
	print ("Starting loop")

	while True:
		time.sleep(1)




if __name__ == "__main__":
	main()
