import cherrypy
import json
import threading
import os
import requests
import shutil
import tarfile
import time
import signal
import logging
import re
from zeroconf import ServiceBrowser, Zeroconf
import netifaces

CONFIG_FILE="config.json"
LOG_FILE="server.log"




class RepoReleases:

    def __init__(self, owner, repo):

        self._repo=repo
        self._owner=owner

        self._releases=[]
        self._prereleases=[]
        self._legacyRelease=[]

        self._running=False
        self._stop=False
        self._polling=False
        self._streaming=False

        self._mdnshosts=[]
        self._legacyhosts=[]


        self._poller = threading.Thread(target=self.fetchAssetsTimed_thread, args=(120,))
        self._zerconf = threading.Thread(target=self.findDevices_thread, args=(60,))

        self._poller.start()
        self._zerconf.start()

        self.loadConfig()


    def loadConfig(self):

        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE) as json_file:
                self._config=json.load(json_file)        
        else:
            self._config={ "manifest":{} }

        

    def saveConfig(self):

        with open(CONFIG_FILE, 'w') as outfile:
            json.dump(self._config, outfile, indent=4)




    # do this once-ish
    # fetch all available releases
    def gather(self):

        logging.info("Gathering ...")

        # clean up
        self._releases=[]
        self._prereleases=[]
        self._legacyRelease=[]

        # this gets all pre/releases - draft too
        releases=self.fetchListOfAllReleases()

        # walk thru them
        if releases is None:
            logging.warning("no releases found")
            return

        for eachRelease in releases:

            # we ignore all drafts
            if "draft" in eachRelease and eachRelease["draft"]==True:
                logging.debug("Skipping DRAFT {}".format(eachRelease["tag_name"]))
                continue

            if "prerelease" in eachRelease and eachRelease["prerelease"]==True:
                self._prereleases.append(eachRelease)
            else:
                self._releases.append(eachRelease)
            
        logging.info("Found {} releases, {} pre-releases".format(len(self._releases),len(self._prereleases)))

        # then get legacy
        legacy=self.fetchSingleRelease()

        if legacy is not None:
            self._legacyRelease.append(legacy)


    def fetchSingleRelease(self):
        # v0.0.27
        url="https://api.github.com/repos/{}/{}/releases/26215342".format(self._owner,self._repo)

        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logging.error("{} returned {}".format(url, req.status_code))

        return None


    def fetchListOfAllReleases(self):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases".format(self._owner,self._repo)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logging.error("{} returned {}".format(url, req.status_code))

        return None

    # deprecated
    def fetchReleaseAssets(self, release):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases/{}/assets".format(self._owner,self._repo,release)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logging.error("{} returned {}".format(url, req.status_code))

        return None


    def downloadReleaseAsset(self, release, dir):

        # always ensure dir is there
        if not os.path.exists(dir) or not os.path.isdir(dir):
            logging.debug("Creating directory %s", dir)
            os.mkdir(dir)

        self._config["manifest"][dir]["tag_name"]=release["tag_name"]
        self._config["manifest"][dir]["files"]=[]

        # has assets
        if "assets" in release and len(release["assets"])>0:
            for eachAsset in release["assets"]:

                url=eachAsset["browser_download_url"]

                with requests.get(url, stream=True) as req:
        
                    if req.status_code==200:

                        filename=eachAsset["name"]

                        logging.debug("Creating file {}".format(filename))

                        with open(filename, 'wb') as fd:
                            #fd.write(req.content)
                            shutil.copyfileobj(req.raw, fd)

                        # check it
                        if tarfile.is_tarfile(filename):

                            # detar into pd
                            tf=tarfile.open(filename)

                            tf.extractall(dir)


                            for member in tf.getmembers():
                                self._config["manifest"][dir]["files"].append(member.name)

                            logging.debug("Deleting file {}".format(filename))
                            os.remove(filename)

                            logging.debug(self._config["manifest"][dir])


                        else:
                            logging.error("{} is not a tarfile".format(filename))
                    else:
                        logging.error("HTTP error {}".format(req.status_code))
                    
        else:
            logging.error("no assets for {} '{}'".format(dir, release["name"]))



    def _downloadIt(self, list, dir):

        logging.info("downloadit {}".format(dir))

        if len(list)>0:

            topRelease=list[0]

            # check we haven't already got this
            if dir not in self._config["manifest"] or "tag_name" not in self._config["manifest"][dir] or topRelease["tag_name"] != self._config["manifest"][dir]["tag_name"]:

                #sanity check the tag
                if self.crackVersion(topRelease["tag_name"]) is None:
                    logging.error("version is malformed {}".format(topRelease["tag_name"]))
                else:
                    # we should clean up
                    if dir in self._config["manifest"] and "files" in self._config["manifest"][dir]:
                        for eachFile in self._config["manifest"][dir]["files"]:
                            logging.info("removing {}".format(eachFile))
                            filetokill=dir+"/"+eachFile
                            if os.path.exists(filetokill):
                                os.remove(dir+"/"+eachFile)

                    self._config["manifest"][dir]={}

                    self.downloadReleaseAsset(topRelease,dir)

                    self.saveConfig()

            else:
                logging.info("{} {} assets already downloaded".format(dir, topRelease["tag_name"]))


        else:
            logging.warning("Nothing to download for {}".format(dir))


    def downloadLatestAssets(self):

        # do a gather
        self.gather()

        # work out the newest release, and prerelease
        self._downloadIt(self._releases,"releases")
        self._downloadIt(self._prereleases,"prereleases")
        self._downloadIt(self._legacyRelease,"legacy")




    def stopPoller(self):

        if self._running==True:

            logging.debug("requesting poll stop ...")
            self._stop=True
            
            while self._running==True:
                pass

            logging.debug("poll stopped!")


    def remove_service(self, zeroconf, type, name):
        logging.info("Service {} removed".format(name))

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        logging.info("Service {} add_service info {}".format(name, info))


        def addMDNShost(hosts, info):
            alreadyThere=[x for x in hosts if x["server"] == info.server]
            if len(alreadyThere)==0:
                for address in info.addresses:
                    stringAddress="{}.{}.{}.{}".format(address[0], address[1], address[2],address[3])
                    logging.info(stringAddress)
                    hosts.append({"server":info.server,"address": stringAddress})
            else:
                logging.info("already in list")


        # carve up the addresses
        if info is not None:

            # look for legacy
            if info.type=="_barneyman._tcp.local.":
                logging.info("Adding mdns")
                addMDNShost(self._mdnshosts, info)
            else:
                logging.info("Adding legacy")
                addMDNShost(self._legacyhosts, info)



    # threaded functions
    def findDevices_thread(self, timeoutSeconds):

        logging.critical("findDevices_thread started ...")

        zeroconf = Zeroconf()

        browser = ServiceBrowser(zeroconf, "_barneyman._tcp.local.", self)
        #legacy = ServiceBrowser(zeroconf, "_bjfLights._tcp.local.", self)

        while not self._stop:

            time.sleep(timeoutSeconds)

        zeroconf.close()        


    def fetchAssetsTimed_thread(self, timeoutMinutes):

        logging.critical("fetchAssetsTimed_thread started ...")

        self._running=True

        self._lastPoll=None

        while not self._stop:

            if self._lastPoll is None or ((time.time()-self._lastPoll)>timeoutMinutes*60):

                logging.debug("Doing a poll")

                self._polling=True

                self.downloadLatestAssets()

                self._polling=False

                self._lastPoll=time.time()

                # then ask all devices to upgrade

                self.upgradeAllDevices()

            time.sleep(5)

        self._running=False

        logging.critical("fetchAssetsTimed_thread stopping ...")

    def upgradeAllDevices(self, legacy=False):
        
        for host in self._mdnshosts:

            upgradeUrl="http://{}//json/upgrade".format(host["address"])

            myIP=cherrypy.server.socket_host
            myPort=cherrypy.server.socket_port

            if legacy==True:
                body={"url":"/upgradeBinary","host":myIP,"port":myPort}
            else:
                body={"url":"http://{}:{}/upgradeBinary".format(myIP,myPort),"urlSpifs":"http://{}:{}/upgradeFiles".format(myIP,myPort)}

            logging.critical("calling {} with {}".format(upgradeUrl, body))

            try:
                req=requests.post(upgradeUrl, body)

                if req.status_code==200:
                    pass                

            except Exception as e:
                logging.error(e)
            


    def vgreater(self, earlier, later):

        # major
        if earlier["version"][0] > later["version"][0]:
            return False
        elif earlier["version"][0] < later["version"][0]:
            return True

        #major is same
        #minor
        if earlier["version"][1] > later["version"][1]:
            return False
        elif earlier["version"][1] < later["version"][1]:
            return True

        #minor is same
        #build
        #debug
        if earlier["version"][2] > later["version"][2]:
            return False
        elif earlier["version"][2] < later["version"][2]:
        # following line to redownload the same f/w            
        #elif earlier["version"][2] <= later["version"][2]:
            return True

        return False
        


    def crackVersion(self,vstring):

        versions=re.match("(v\\d+\\.\\d+\\.\\d+)\\.?(pr)?", vstring)

        if versions is None:
            return None

        if len(versions.group())!=len(vstring):        
            return None

        preRelease=True if versions.group(2) is not None else False

        # then crack the number
        cracked=re.match("v(\\d+)\\.(\\d+)\\.(\\d+)", vstring)

        ret={ "version": [ int(cracked.group(1)),int(cracked.group(2)),int(cracked.group(3)) ], "prerelease": preRelease }

        logging.debug("Cracked {} to {}".format(vstring, ret))

        return ret

            
    # web methods
    @cherrypy.expose
    def version(self):
        return "v0"

    @cherrypy.expose
    def manifest(self):
        return json.dumps(self._config, indent=4)    

    @cherrypy.expose
    def updateBinary(self):
        return self.sendUpdateFile("bin")

    @cherrypy.expose
    def updateFiles(self):
        return self.sendUpdateFile("spiffs")


    def sendUpdateFile(self, fileTail):
        # needs headers
        # HTTP_X_ESP8266_VERSION

        # cherrypy uses TitleCase
        currentVer = cherrypy.request.headers.get('X-Esp8266-Version')
        userAgent=cherrypy.request.headers.get('User-Agent')

        logging.info("request {} ver {}".format(userAgent, currentVer))

        if userAgent is None or userAgent!="ESP8266-http-Update":
            cherrypy.response.status=403
            logging.warning("HTTPUpdate - Wrong User Agent")
            return "Error - Wrong User Agent"

        if currentVer is None:
            cherrypy.response.status=406
            logging.warning("HTTPUpdate - Error - no version")
            return "Error - no version"

        legacy=False
        # arooga - special, legacy case
        if currentVer.startswith("lightS_"):
            currentVer="sonoff_basic|v0.0.0"
            legacy=True


        # carve that up
        hardware = currentVer.split("|")
        if len(hardware)!=2:
            # not expected
            cherrypy.response.status=406
            logging.warning("HTTPUpdate - Error - malformed hardware|version {}".format(currentVer))
            return "Error - malformed hardware|version"

        # then carve it up
        versions=self.crackVersion(hardware[1])

        if versions is None:
            # malformed 
            cherrypy.response.status=406
            logging.warning("HTTPUpdate - Error - malformed version")
            return "Error - malformed version"

        # check if we're polling
        if self._polling==True:
            logging.warning("HTTPUpdate -Busy")
            cherrypy.response.status=503
            return "Busy"

        if legacy==True:
            dir="legacy"
        else:
            dir="prereleases" if versions["prerelease"]==True else "releases"

        Node = self._config["manifest"][dir]

        if not self.vgreater(versions,self.crackVersion(Node["tag_name"])):
            cherrypy.response.status=304
            logging.warning("HTTPUpdate - No upgrade")
            return "No upgrade"


        # now we have to carve the hardware
        lookfor=hardware[0]+"_.*\\."+fileTail+"$"
        r=re.compile(lookfor)
        newlist = list(filter(r.match, Node["files"])) # Read Note

        #should only be one candidate
        if len(newlist)!=1:
            cherrypy.response.status=500
            logging.warning("HTTPUpdate - No candidate")
            return "No candidate"


        macAddress = cherrypy.request.headers.get('Http-X-Esp8266-Sta-Mac')

        logging.info(cherrypy.request.headers)
        logging.info("Heard from {} - {}".format(currentVer, macAddress) )


        name =os.path.abspath("./"+dir+"/"+newlist[0])

        logging.info("returning {}".format(name))

        basename = os.path.basename(name)
        filename = name
        mime     = 'application/octet-stream'
        return cherrypy.lib.static.serve_file(filename, mime, basename)




os.remove(LOG_FILE)
logging.basicConfig(filename=LOG_FILE,level=logging.DEBUG)



# entry point
if __name__ == '__main__':

    myrels=RepoReleases("barneyman","ESP8266-Light-Switch")

    def signal_handler(sig, frame):

        logging.info("Detected SIGINT")
        # stop my thread
        myrels.stopPoller()


    # handle sigint
    # signal.signal(signal.SIGINT, signal_handler)



    try:
        #app.run(debug=True, host='0.0.0.0', port=8084)

        netifaces.ifaddresses('eth0')
        ip = netifaces.ifaddresses('eth0')[netifaces.AF_INET][0]['addr']        

        cherrypy.server.socket_host = ip #'0.0.0.0' # put it here 
        cherrypy.quickstart(myrels)

        while True:
            pass

    except Exception as e:

        logging.error(e)


    myrels.stopPoller()

    logging.critical("Exiting main")

