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
import sys
import socket
from zeroconf import ServiceBrowser, Zeroconf

DATA_STEM="/data"

CONFIG_FILE="./server_config.json"
HA_ADDON_CONFIG_FILE="/data/options.json"

#LOG_FILE=DATA_STEM+"./server.log"
LOG_FILE=None



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
        self._updatePending=False

        self._mdnshosts=[]
        self._legacyhosts=[]


        self._poller = threading.Thread(target=self.fetchAssetsTimed_thread, args=())
        self._zerconf = threading.Thread(target=self.findDevices_thread, args=(2,))

        self._zerconf.start()
        self._poller.start()

        self.loadConfig()

        logger.critical("Setting logging to '{}'".format(self._haconfig["logging"]))

        logger.setLevel(self._haconfig["logging"])

        #logger.critical("logging now at '{}'".format(logger.getLevelName(logger.level)))

    def loadConfig(self):

        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE) as json_file:
                self._config=json.load(json_file)        
        else:
            self._config={ "manifest":{} }

        self.loadHAconfig()


    def loadHAconfig(self):
        # this file will be populated by HA when run as an addon
        # it's the options in config.json
        if os.path.isfile(HA_ADDON_CONFIG_FILE):
            with open(HA_ADDON_CONFIG_FILE) as json_file:
                self._haconfig=json.load(json_file)        
        else:
            self._haconfig={ "host":"hassio", "logging":"DEBUG","prerelease":True, "release":True,"legacy":True, "port":8080, "poll":15 }


    def port(self):
        return self._haconfig["port"]

    def saveConfig(self):

        with open(CONFIG_FILE, 'w') as outfile:
            json.dump(self._config, outfile, indent=4)




    # do this once-ish
    # fetch all available releases
    def gather(self):

        logger.info("Gathering ...")

        # clean up
        self._releases=[]
        self._prereleases=[]
        self._legacyRelease=[]

        # this gets all pre/releases - draft too
        releases=self.fetchListOfAllReleases()

        # walk thru them
        if releases is None:
            logger.warning("no releases found")
            return

        for eachRelease in releases:

            # we ignore all drafts
            if "draft" in eachRelease and eachRelease["draft"]==True:
                logger.debug("Skipping DRAFT {}".format(eachRelease["tag_name"]))
                continue

            if "prerelease" in eachRelease and eachRelease["prerelease"]==True:
                self._prereleases.append(eachRelease)
            else:
                self._releases.append(eachRelease)
            
        logger.info("Found {} releases, {} pre-releases".format(len(self._releases),len(self._prereleases)))

        # then get legacy
        legacy=self.fetchSingleRelease(26335159)

        if legacy is not None:
            self._legacyRelease.append(legacy)


    def fetchSingleRelease(self,revision):
        # v0.0.27
        url="https://api.github.com/repos/{}/{}/releases/{}".format(self._owner,self._repo,revision)

        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("{} returned {}".format(url, req.status_code))

        return None


    def fetchListOfAllReleases(self):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases".format(self._owner,self._repo)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("{} returned {}".format(url, req.status_code))

        return None

    # deprecated
    def fetchReleaseAssets(self, release):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases/{}/assets".format(self._owner,self._repo,release)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("{} returned {}".format(url, req.status_code))

        return None


    def downloadReleaseAsset(self, release, dir):

        osdir=os.path.join(DATA_STEM,dir)

        # always ensure dir is there
        if not os.path.exists(dir) or not os.path.isdir(dir):
            logger.debug("Creating directory %s", dir)
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

                        logger.debug("Creating file {}".format(filename))

                        with open(filename, 'wb') as fd:
                            #fd.write(req.content)
                            shutil.copyfileobj(req.raw, fd)

                        # check it
                        if tarfile.is_tarfile(filename):

                            # detar into pd
                            tf=tarfile.open(filename)

                            tf.extractall(osdir)

                            for member in tf.getmembers():
                                self._config["manifest"][dir]["files"].append(member.name)

                            logger.debug("Deleting file {}".format(filename))
                            os.remove(filename)

                            logger.debug(self._config["manifest"][dir])


                        else:
                            logger.error("{} is not a tarfile".format(filename))
                    else:
                        logger.error("HTTP error {}".format(req.status_code))
                    
        else:
            logger.error("no assets for {} '{}'".format(dir, release["name"]))



    def _downloadIt(self, list, dir):

        logger.info("Fetching {}".format(dir))

        if len(list)>0:

            topRelease=list[0]

            # check we haven't already got this
            if dir not in self._config["manifest"] or "tag_name" not in self._config["manifest"][dir] or topRelease["tag_name"] != self._config["manifest"][dir]["tag_name"]:

                #sanity check the tag
                if self.crackVersion(topRelease["tag_name"]) is None:
                    logger.error("version is malformed {}".format(topRelease["tag_name"]))
                else:
                    # we should clean up
                    if dir in self._config["manifest"] and "files" in self._config["manifest"][dir]:
                        for eachFile in self._config["manifest"][dir]["files"]:
                            logger.info("removing {}".format(eachFile))
                            filetokill=dir+"/"+eachFile
                            if os.path.exists(filetokill):
                                os.remove(dir+"/"+eachFile)

                    self._config["manifest"][dir]={}

                    self.downloadReleaseAsset(topRelease,dir)

                    self.saveConfig()

                    # shits changed yo, worth an update loop
                    self._updatePending=True

            else:
                logger.info("{} {} assets already downloaded".format(dir, topRelease["tag_name"]))


        else:
            logger.warning("Nothing to download for {}".format(dir))


    def downloadLatestAssets(self):

        # do a gather
        self.gather()

        # work out the newest release, and prerelease
        if self._haconfig["release"]==True:
            self._downloadIt(self._releases,"releases")
        if self._haconfig["prerelease"]==True:
            self._downloadIt(self._prereleases,"prereleases")
        if self._haconfig["legacy"]==True:
            self._downloadIt(self._legacyRelease,"legacy")




    def stopPoller(self):

        if self._running==True:

            logger.debug("requesting poll stop ...")
            self._stop=True
            
            while self._running==True:
                pass

            logger.debug("poll stopped!")


    def update_service(self, zeroconf, type, name):
        logger.debug("Service {} updated".format(name))
        # it's possible the version has changed, catch that
        self.add_service(zeroconf, type, name)

    def remove_service(self, zeroconf, type, name):
        logger.info("Service {} removed".format(name))
        info = zeroconf.get_service_info(type, name)

        def delMDNShost(hosts,name, info):

            for each in hosts:
                if each["name"]==name:
                    hosts.remove(each)
                    return


        delMDNShost(self._mdnshosts,name, info)
        

    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        logger.info("Service {} add_service info {}".format(name, info))


        def addMDNShost(hosts, name, info):
            alreadyThere=[x for x in hosts if x["server"] == info.server]

            # work out string address
            address=info.addresses[0]
            stringAddress="{}.{}.{}.{}".format(address[0], address[1], address[2],address[3])
            logger.debug("{} ip {}".format(info.server,stringAddress))
            hostversion=None
            if b"version" in info.properties:
                hostversion=info.properties[b"version"].decode("UTF8")

            if len(alreadyThere)!=0:
                logger.debug("updating")
                alreadyThere[0]["address"]=stringAddress
                if hostversion is not None:
                    alreadyThere[0]["version"]=hostversion
            else:
                logger.debug("adding")
                newhost={"name":name, "server":info.server,"address": stringAddress}
                # check for version in properties - props is utf8, so decode
                if b"version" in info.properties:
                    newhost["version"]=info.properties[b"version"].decode("UTF8")
                logger.debug(newhost)
                hosts.append(newhost)



        # carve up the addresses
        if info is not None:

            # look for legacy
            if info.type=="_barneyman._tcp.local.":
                logger.debug("Adding mdns")
                addMDNShost(self._mdnshosts,name, info)
            else:
                logger.debug("Adding legacy")
                addMDNShost(self._legacyhosts,name, info)



    # threaded functions
    def findDevices_thread(self, timeoutMinutes):

        logger.critical("findDevices_thread started ...")

        zeroconf = Zeroconf()

        browser = ServiceBrowser(zeroconf, "_barneyman._tcp.local.", self)
        
        while not self._stop:
            time.sleep(10)
            pass

        zeroconf.close()        


    def fetchAssetsTimed_thread(self):

        logger.critical("fetchAssetsTimed_thread started ...")

        self._running=True

        self._lastPoll=None

        while not self._stop:

            if self._updatePending or (self._lastPoll is None or ((time.time()-self._lastPoll)>self._haconfig["poll"]*60)):

                logger.debug("Doing a download/upgrade poll")

                self._polling=True

                self.downloadLatestAssets()

                self._polling=False

                self._lastPoll=time.time()

                # then ask all devices to upgrade
                self.upgradeAllDevices()
                
                self._updatePending=False

            time.sleep(60)

        self._running=False

        logger.critical("fetchAssetsTimed_thread stopping ...")

    def upgradeAllDevices(self, legacy=False):

        logger.info("calling upgradeAllDevices with {} devices".format(len(self._mdnshosts)))
        
        for host in self._mdnshosts:

            # quick optimisation, if there is a version, crack it early
            if "version" in host:
                hardware = host["version"].split("|")
                deviceVersion=self.crackVersion(hardware[1])

                doupdate=False

                if self._haconfig["prerelease"] and deviceVersion["prerelease"]:
                    # check the top prerelease
                    toppre=self.crackVersion( self._prereleases[0]["tag_name"])
                    doupdate=self.vgreater(deviceVersion,toppre,True)
                    logger.debug("mdns ver {} prerel {} result {}".format(deviceVersion,toppre,doupdate))

                if not doupdate:
                    toprel=self.crackVersion( self._releases[0]["tag_name"])
                    doupdate=self.vgreater(deviceVersion,toprel,True)
                    logger.debug("mdns ver {} rel {} result {}".format(deviceVersion,toprel,doupdate))

                if not doupdate:
                    logger.debug("optimised out an update for {}".format(host["address"]))
                    continue


            upgradeUrl="http://{}/json/upgrade".format(host["address"])

            # while running as an HA addon there's a config at /data/options.json
            # which is populated from options in config.json

            myIP=myrels._haconfig["host"]

            myPort=cherrypy.server.socket_port

            if legacy==True:
                body={"url":"/updateBinary","host":myIP,"port":myPort}
            else:
                body={"url":"http://{}:{}/updateBinary".format(myIP,myPort),"urlSpiffs":"http://{}:{}/updateSpiffs".format(myIP,myPort)}

            body=json.dumps(body)

            logger.debug("calling {} with {}".format(upgradeUrl, body))


            try:
                # Content-Type: text/plain 
                req=requests.post(upgradeUrl, body, headers={'Content-Type':'text/plain'})

                if req.status_code==200:
                    # stop us getting bombarded
                    time.sleep(10)
                else:
                    logger.error("Response to UpgradeYourself was {} - Upgrade Only When Off, or refusing pre-rels?".format(req.status_code))



            except Exception as e:
                logger.error(e)
            


    def vgreater(self, earlier, later, prerelease):

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
        if earlier["version"][2] > later["version"][2]:
            return False

        # debug
        elif earlier["version"][2] < later["version"][2]:
            return True
        # if they're the same version, but the device has the PR migrate to the release
        elif (earlier["version"][2] == later["version"][2]) and earlier["prerelease"]==True and later["prerelease"]==False:
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

        logger.debug("Cracked {} to {}".format(vstring, ret))

        return ret

            
    # web methods
    @cherrypy.expose
    def version(self):
        return "v0"

    @cherrypy.expose
    def manifest(self):
        return json.dumps(self._config, indent=4)    

    @cherrypy.expose
    def hosts(self):
        return json.dumps(self._mdnshosts, indent=4)    

    @cherrypy.expose
    def updateBinary(self,**params):
        return self.sendUpdateFile("bin")

    @cherrypy.expose
    def updateSpiffs(self,**params):
        return self.sendUpdateFile("spiffs")

    @cherrypy.expose
    def upgradeAll(self):
        self._updatePending=True
        return 


    def sendUpdateFile(self, fileTail):
        # needs headers
        # HTTP_X_ESP8266_VERSION

        # cherrypy uses TitleCase
        currentDeviceVer = cherrypy.request.headers.get('X-Esp8266-Version')
        userAgent=cherrypy.request.headers.get('User-Agent')

        logger.debug("request {} ver {}".format(userAgent, currentDeviceVer))

        prereleaseOverride=False
        prereleaseRequested=False
        
        # if we are hosting prerels
        if self._haconfig["prerelease"]:
            #get the arg
            if cherrypy.request.params.get("prerelease") is not None:
                prereleaseOverride=True
                if cherrypy.request.params.get("prerelease")=="true":
                    prereleaseRequested=True
                else:
                    prereleaseRequested=False
        else:
            prereleaseOverride=True


        if userAgent is None or userAgent!="ESP8266-http-Update":
            cherrypy.response.status=403
            logger.error("HTTPUpdate - Wrong User Agent")
            return "Error - Wrong User Agent"

        if currentDeviceVer is None:
            cherrypy.response.status=406
            logger.error("HTTPUpdate - Error - no version")
            return "Error - no version"

        legacy=False
        # arooga - special, legacy case
        if currentDeviceVer.startswith("lightS_"):
            currentDeviceVer="sonoff_basic|v0.0.0"
            legacy=True


        # carve that up
        hardware = currentDeviceVer.split("|")
        if len(hardware)!=2:
            # not expected
            cherrypy.response.status=406
            logger.warning("HTTPUpdate - Error - malformed hardware|version {}".format(currentDeviceVer))
            return "Error - malformed hardware|version"

        # then carve it up
        deviceVersion=self.crackVersion(hardware[1])

        # check for prerelease request
        if prereleaseOverride is True:
            logger.debug("Prerelease override {}".format(prereleaseRequested))


        if deviceVersion is None:
            # malformed 
            cherrypy.response.status=406
            logger.warning("HTTPUpdate - Error - malformed version")
            return "Error - malformed version"


        # check if we're polling
        if self._polling==True:
            logger.warning("HTTPUpdate - Busy")
            cherrypy.response.status=503
            return "Busy"

        # sort out which branch to pass to them
        if legacy==True:
            dir="legacy"
        else:
            if prereleaseOverride==False:
                dir="prereleases" if deviceVersion["prerelease"]==True else "releases"
            else:
                dir="prereleases" if prereleaseRequested==True else "releases"

        Node = self._config["manifest"][dir]

        if not self.vgreater(deviceVersion,self.crackVersion(Node["tag_name"]), (prereleaseRequested if prereleaseOverride==True else None)):
            cherrypy.response.status=304
            logger.info("HTTPUpdate - No upgrade")
            return "No upgrade"


        # now we have to carve the hardware
        lookfor=hardware[0]+"_.*\\."+fileTail+"$"
        r=re.compile(lookfor)
        newlist = list(filter(r.match, Node["files"])) # Read Note

        #should only be one candidate
        if len(newlist)!=1:
            cherrypy.response.status=500
            logger.warning("HTTPUpdate - No candidate")
            return "No candidate"


        macAddress = cherrypy.request.headers.get('X-Esp8266-Sta-Mac')
        #logger.info(cherrypy.request.headers)
        logger.info("Heard from {} - {}".format(currentDeviceVer, macAddress) )

        name= os.path.join(os.path.join(DATA_STEM,dir),newlist[0])

        logger.info("returning {}".format(name))

        basename = os.path.basename(name)
        filename = name
        mime     = 'application/octet-stream'
        return cherrypy.lib.static.serve_file(filename, mime, basename)


if LOG_FILE is not None:

    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)

    logging.basicConfig(filename=LOG_FILE,level=logging.WARNING,format='%(asctime)s:%(levelname)s:%(message)s')

else:
    logging.basicConfig(stream=sys.stdout,level=logging.WARNING,format='%(asctime)s:%(levelname)s:%(message)s')

    # define a Handler which writes INFO messages or higher to the sys.stderr
    logger=logging.getLogger("updater")
    console = logging.StreamHandler()
    
    

# entry point
if __name__ == '__main__':

    myrels=RepoReleases("barneyman","ESP8266-Light-Switch")

    def signal_handler(sig, frame):

        logger.warning("Detected SIGINT")
        # stop my thread
        myrels.stopPoller()



    try:

        cherrypy.config.update({'server.socket_port': myrels.port()})
        cherrypy.config.update({'server.socket_host' : '0.0.0.0'})

        cherrypy.quickstart(myrels)

        while True:
            time.sleep(60)

    except Exception as e:

        logger.error(e)

    logger.warning("exiting")

    myrels.stopPoller()

    logger.critical("Exiting main")

