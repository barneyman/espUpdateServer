import cherrypy
import json
import threading
import os
import signal
import requests
import shutil
import tarfile
import time
import logging
import re
import sys
import zipfile
from zeroconf import ServiceBrowser, Zeroconf

DATA_STEM="/data"

CONFIG_FILE="/data/server_config.json"
HA_ADDON_CONFIG_FILE="/data/options.json"

#LOG_FILE=DATA_STEM+"./server.log"
LOG_FILE=None



class RepoReleases:

    def __init__(self, owner, repo):

        self._repo=repo
        self._owner=owner

        self._releases=[]
        self._prereleases=[]
        self._nightly=[]

        self._fetch_running=False
        self._stop=False
        self._polling=False
        self._streaming=False
        self._updatePending=False

        self._mdnshosts=[]

        self.loadConfig()

        self._zeroRunninng=False
        self._poller = threading.Thread(target=self.fetchAssetsTimed_thread, args=())
        self._zero_conf = threading.Thread(target=self.findDevices_thread, args=(2,))

        self._zero_conf.start()
        self._poller.start()


        logger.critical("Setting logging to '%s'",self._haconfig["logging"])

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
            self._haconfig={ "host":"debian", "logging":"DEBUG","nightly":True, "release":True,"port":8080, "poll":15 }


    def port(self):
        return self._haconfig["port"]

    def saveConfig(self):

        with open(CONFIG_FILE, 'w') as outfile:
            json.dump(self._config, outfile, indent=4)




    # do this once-ish
    # fetch all available releases
    def gather(self):

        logger.info("Gathering assets from Github ...")

        # clean up
        self._releases=[]
        self._prereleases=[]

        runs=self.FetchActionRuns()
        nightlys=self.fetchActionArtifacts()

        if nightlys is not None and runs is not None:
        
            lastRun=runs["workflow_runs"][0]

            if lastRun["conclusion"]=="success" and lastRun["event"]=="push":

                # now look for assets for it
                for each in nightlys["artifacts"]:
                    if each["workflow_run"]["id"]==lastRun["id"]:
                        for types in ["wemosD1", "sonoff_basic", "esp32_cam"]:
                            if each["name"].find(types)!=-1: 
                                self._nightly.append(each)
                                break

        # this gets all pre/releases - draft too
        releases=self.fetchListOfAllReleases()

        # walk thru them
        if releases is None:
            logger.warning("no releases found")
            return

        for eachRelease in releases:

            # we ignore all drafts
            if "draft" in eachRelease and eachRelease["draft"]==True:
                logger.debug("Skipping DRAFT %s",eachRelease["tag_name"])
                continue

            if "prerelease" in eachRelease and eachRelease["prerelease"]==True:
                logger.debug("Found prerelease %s",eachRelease["tag_name"])
                self._prereleases.append(eachRelease)
            else:
                logger.debug("Found release %s",eachRelease["tag_name"])
                self._releases.append(eachRelease)
            
        logger.info("Found %s releases, %s pre-releases",len(self._releases),len(self._prereleases))



    def fetchSingleRelease(self,revision):
        # v0.0.27
        url="https://api.github.com/repos/{}/{}/releases/{}".format(self._owner,self._repo,revision)

        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("fetchSingleRelease : %s returned %s",url, req.status_code)

        return None

    def FetchActionRuns(self):
        url="https://api.github.com/repos/{}/{}/actions/runs".format(self._owner,self._repo)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("FetchActionRuns : %s returned %s",url, req.status_code)
        return None

    def fetchActionArtifacts(self):
        # build the url
        url="https://api.github.com/repos/{}/{}/actions/artifacts".format(self._owner,self._repo)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("fetchActionArtifacts : %s returned %s",url, req.status_code)
        return None


    def fetchListOfAllReleases(self):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases".format(self._owner,self._repo)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("fetchListOfAllReleases : %s returned %s",url, req.status_code)

        return None

    # deprecated
    def fetchReleaseAssets(self, release):
        # build the url
        url="https://api.github.com/repos/{}/{}/releases/{}/assets".format(self._owner,self._repo,release)

        # get that as json
        req=requests.get(url)
        
        if req.status_code==200:
            return req.json()

        logger.error("fetchReleaseAssets : %s returned %s",url, req.status_code)

        return None


    def downloadReleaseAsset(self, release, asset_dir):

        osdir=os.path.join(DATA_STEM,asset_dir)

        # always ensure asset_dir is there
        if not os.path.exists(osdir) or not os.path.isdir(osdir):
            logger.debug("Creating directory %s", osdir)
            os.mkdir(osdir)

        self._config["manifest"][asset_dir]["tag_name"]=release["tag_name"]
        self._config["manifest"][asset_dir]["files"]=[]

        # has assets
        if "assets" in release and len(release["assets"])>0:
            for eachAsset in release["assets"]:

                url=eachAsset["browser_download_url"]

                with requests.get(url, stream=True) as req:
        
                    if req.status_code==200:

                        filename="/tmp/"+eachAsset["name"]

                        logger.debug("Creating file %s",filename)

                        with open(filename, 'wb') as fd:
                            #fd.write(req.content)
                            shutil.copyfileobj(req.raw, fd)

                        # check it
                        if tarfile.is_tarfile(filename):

                            # detar into pd
                            tf=tarfile.open(filename)

                            tf.extractall(osdir)

                            for member in tf.getmembers():
                                self._config["manifest"][asset_dir]["files"].append(member.name)

                            logger.debug("Deleting file %s",filename)
                            os.remove(filename)

                            logger.debug(self._config["manifest"][asset_dir])


                        else:
                            logger.error("%s is not a tarfile",filename)
                    else:
                        logger.error("HTTP error %s",str(req.status_code))
                    
        else:
            logger.error("no assets for %s '%s'",asset_dir, release["name"])

    def _download_artifacts(self):

        osdir=os.path.join(DATA_STEM,"nightly")
        if not os.path.exists(osdir) or not os.path.isdir(osdir):
            logger.debug("Creating directory %s", osdir)
            os.mkdir(osdir)


        if len(self._nightly):
            tagName=self._nightly[0]["name"].split("-")[1]

            if "nightly" not in self._config["manifest"] or "tag_name" not in self._config["manifest"]["nightly"] or self._config["manifest"]["nightly"]["tag_name"]!=tagName:

                self._config["manifest"]["nightly"]={}
                self._config["manifest"]["nightly"]["files"]=[]

                self._config["manifest"]["nightly"]["tag_name"]=tagName


                for each in self._nightly:

                    # /repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}
                    url="https://api.github.com/repos/{}/{}/actions/artifacts/{}/zip".format(self._owner,self._repo,each["id"])

                    #Authorization: token $PERSONAL_TOKEN
                    req=requests.get(url, headers={"Authorization":"token "+self._haconfig["token"]}, stream=True)

                    if req.status_code==200:

                        with open("/tmp/tmp.zip","wb") as zip:
                            shutil.copyfileobj(req.raw,zip)
                            zip.close()

                            if zipfile.is_zipfile("/tmp/tmp.zip"):
                                with zipfile.ZipFile("/tmp/tmp.zip") as unzip:
                                    unzip.extractall(osdir)

                                tarname=os.path.join(osdir,unzip.filelist[0].filename)

                                # should be a tar.gz
                                tf=tarfile.open(tarname)
                                tf.extractall(osdir)
                                for member in tf.getmembers():
                                    self._config["manifest"]["nightly"]["files"].append(member.name)
                                tf.close()
                                os.unlink(tarname)


                            os.unlink("/tmp/tmp.zip")
                self.saveConfig()

    def _clean_up(self, asset_dir):

        if asset_dir in self._config["manifest"] and "files" in self._config["manifest"][asset_dir]:
            for eachFile in self._config["manifest"][asset_dir]["files"]:
                logger.info("removing %s",eachFile)
                filetokill=asset_dir+"/"+eachFile
                if os.path.exists(filetokill):
                    os.remove(asset_dir+"/"+eachFile)


    def _download_asset(self, asset_list, asset_dir):

        logger.info("Fetching %s",asset_dir)

        if len(asset_list)>0:

            topRelease=asset_list[0]

            # check we haven't already got this
            if asset_dir not in self._config["manifest"] or "tag_name" not in self._config["manifest"][asset_dir] or topRelease["tag_name"] != self._config["manifest"][asset_dir]["tag_name"]:

                #sanity check the tag
                if self.crackVersion(topRelease["tag_name"]) is None:
                    logger.error("version is malformed %s",topRelease["tag_name"])
                else:
                    # we should clean up
                    self._clean_up(asset_dir)

                    self._config["manifest"][asset_dir]={}

                    self.downloadReleaseAsset(topRelease,asset_dir)

                    self.saveConfig()

                    # shits changed yo, worth an update loop
                    self._updatePending=True

            else:
                logger.info("%s %s assets already downloaded",asset_dir, topRelease["tag_name"])


        else:
            logger.warning("Nothing to download for %s",asset_dir)


    def downloadLatestAssets(self):

        # do a gather
        self.gather()

        # work out the newest release, and prerelease
        if self._haconfig["release"]==True:
            self._download_asset(self._releases,"releases")

        if self._haconfig["nightly"]==True:
            self._download_artifacts()


    def stopPoller(self):

        if self._fetch_running==True:

            logger.debug("requesting poll stop ...")
            self._stop=True

            while self._fetch_running and self._zeroRunninng:
                pass

            logger.debug("poll stopped!")


    def update_service(self, zeroconf, service_type, name):
        # it's possible the version has changed, catch that
        self.add_service(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name):
        logger.info("Service %s removed",name)
        info = zeroconf.get_service_info(service_type, name)

        def delMDNShost(hosts,name, info):

            for each in hosts:
                if each["name"]==name:
                    hosts.remove(each)
                    return


        delMDNShost(self._mdnshosts,name, info)
        

    def add_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name)
        logger.debug("Service %s add_service info %s",name, info)


        def addMDNShost(hosts, name, info):
            alreadyThere=[x for x in hosts if x["server"] == info.server]

            # work out string address
            address=info.addresses[0]
            stringAddress="{}.{}.{}.{}".format(address[0], address[1], address[2],address[3])
            logger.debug("%s ip %s",info.server,stringAddress)
            hostversion=None
            if b"version" in info.properties:
                hostversion=info.properties[b"version"].decode("UTF8")

            if len(alreadyThere)!=0:
                logger.info("updating Service %s add_service info %s",name, info)
                alreadyThere[0]["address"]=stringAddress
                if hostversion is not None:
                    alreadyThere[0]["version"]=hostversion
            else:
                logger.info("adding Service %s add_service info %s",name, info)
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
                addMDNShost(self._mdnshosts,name, info)



    # threaded functions
    def findDevices_thread(self, timeoutMinutes):

        logger.critical("findDevices_thread started ...")

        zeroconf = Zeroconf()
        self._zeroRunninng=True

        browser = ServiceBrowser(zeroconf, "_barneyman._tcp.local.", self)
        
        while not self._stop:
            time.sleep(2)
            pass

        zeroconf.close()
        logger.critical("findDevices_thread stopped ...")

        self._zeroRunninng=False

    def fetchAssetsTimed_thread(self):

        logger.critical("fetchAssetsTimed_thread started ...")

        self._fetch_running=True

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

            time.sleep(3)

        logger.critical("fetchAssetsTimed_thread stoped")
        self._fetch_running=False

    def upgradeAllDevices(self):

        logger.info("calling upgradeAllDevices with %s devices",len(self._mdnshosts))
        
        for host in self._mdnshosts:

            # quick optimisation, if there is a version, crack it early
            if "version" in host:
                hardware = host["version"].split("|")
                deviceVersion=self.crackVersion(hardware[1])

                doupdate=True
                # doupdate=False

                # if self._haconfig["prerelease"] and deviceVersion["prerelease"]:
                #     # check the top prerelease
                #     toppre=self.crackVersion( self._prereleases[0]["tag_name"])
                #     doupdate=self.vgreater(deviceVersion,toppre)
                #     logger.debug("mdns ver %s prerel %s result %s",deviceVersion,toppre,doupdate)

                # if not doupdate:
                #     toprel=self.crackVersion( self._releases[0]["tag_name"])
                #     doupdate=self.vgreater(deviceVersion,toprel)
                #     logger.debug("mdns ver %s rel %s result %s",deviceVersion,toprel,doupdate)

                # if not doupdate:
                #     logger.debug("optimised out an update for %s",host["address"])
                #     continue


            upgradeUrl="http://{}/json/upgrade".format(host["address"])

            # while running as an HA addon there's a config at /data/options.json
            # which is populated from options in config.json

            myIP=myrels._haconfig["host"]
            myPort=cherrypy.server.socket_port

            body={"url":"http://{}:{}/updateBinary".format(myIP,myPort),"urlSpiffs":"http://{}:{}/updateSpiffs".format(myIP,myPort)}

            body=json.dumps(body)

            logger.info("Asking %s to update itself",host["address"]) 
            logger.debug("calling %s with %s",upgradeUrl, body)


            try:
                # Content-Type: text/plain 
                req=requests.post(upgradeUrl, body, headers={'Content-Type':'text/plain'})

                if req.status_code==200:
                    # stop us getting bombarded
                    time.sleep(10)
                else:
                    logger.error("Response to UpgradeYourself was %s - Upgrade Only When Off, or refusing pre-rels?",req.status_code)



            except Exception as e:
                logger.error(e)
            


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
        if earlier["version"][2] > later["version"][2]:
            return False

        # debug
        elif earlier["version"][2] < later["version"][2]:
            return True

        return False
        


    def crackVersion(self,vstring):

        # (v\d+\.\d+\.\d+)[\.-]*.*
        versions=re.match("(v\\d+\\.\\d+\\.\\d+)[\\.-]*(.*)", vstring)

        if versions is None:
            return None

        if len(versions.group())!=len(vstring):
            return None

        # then crack the number
        cracked=re.match("v(\\d+)\\.(\\d+)\\.(\\d+)", vstring)

        ret={ "version": [ int(cracked.group(1)),int(cracked.group(2)),int(cracked.group(3)) ] }

        logger.debug("Cracked %s to %s",vstring, ret)

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

    @cherrypy.expose
    def dev_test(self,**params):
        # get the ip
        testdevice=cherrypy.request.params.get("device")

        myIP="192.168.51.112"
        myPort=cherrypy.server.socket_port

        body={"url":"http://{}:{}/dev_updateBinary".format(myIP,myPort),"urlSpiffs":"http://{}:{}/dev_updateSpiffs".format(myIP,myPort)}

        body=json.dumps(body)
        upgradeUrl="http://{}/json/upgrade".format(testdevice)

        logger.info("Asking %s to update itself",testdevice) 
        logger.debug("calling %s with %s",upgradeUrl, body)


        try:
            # Content-Type: text/plain 
            req=requests.post(upgradeUrl, body, headers={'Content-Type':'text/plain'})

            if req.status_code==200:
                # stop us getting bombarded
                time.sleep(10)
            else:
                logger.error("Response to UpgradeYourself was %s - Upgrade Only When Off, or refusing pre-rels?",req.status_code)



        except Exception as e:
            logger.error(e)

        return


    @cherrypy.expose
    def dev_updateBinary(self,**params):
        return self.dev_sendUpdateFile(".bin")

    @cherrypy.expose
    def dev_updateSpiffs(self,**params):
        return self.dev_sendUpdateFile(".spiffs")


    # no frills, force a specific file over - for testing 
    def dev_sendUpdateFile(self, filetail):

        currentDeviceVer = cherrypy.request.headers.get('X-Esp8266-Version')

        if currentDeviceVer is None:
            cherrypy.response.status=406
            return "Missing header"

        userAgent=cherrypy.request.headers.get('User-Agent')

        logger.debug("serve update request %s ver %s",userAgent, currentDeviceVer)

        # carve that up
        hardware = currentDeviceVer.split("|")
        if len(hardware)!=2:
            # not expected
            cherrypy.response.status=406
            logger.warning("HTTPUpdate - Error - malformed hardware|version %s",currentDeviceVer)
            return "Error - malformed hardware|version"

        # dev_hardware="sonoff_basic"
        # if hardware[0]!=dev_hardware:
        #     cherrypy.response.status=406
        #     logger.warning("HTTPUpdate - Error - malformed hardware %s %s",hardware[0],dev_hardware)
        #     return "Error - malformed hardware"


        # then carve it up
        deviceVersion=self.crackVersion(hardware[1])

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

        macAddress = cherrypy.request.headers.get('X-Esp8266-Sta-Mac')
        #logger.info(cherrypy.request.headers)
        logger.info("Heard from %s - %s",currentDeviceVer, macAddress) 

        name= os.path.join("/devtest/",hardware[0])
        name= os.path.join(name,hardware[0]+filetail)

        logger.info("returning %s",name)

        basename = os.path.basename(name)
        filename = name
        mime     = 'application/octet-stream'
        return cherrypy.lib.static.serve_file(filename, mime, basename)



    def sendUpdateFile(self, fileTail):
        # needs headers
        # HTTP_X_ESP8266_VERSION

        # cherrypy uses TitleCase
        currentDeviceVer = cherrypy.request.headers.get('X-Esp8266-Version')
        userAgent=cherrypy.request.headers.get('User-Agent')

        logger.debug("serve update request %s ver %s",userAgent, currentDeviceVer)

        prereleaseRequested=False
        
        # if we are hosting prerels
        if self._haconfig["nightly"]:
            #get the arg
            if cherrypy.request.params.get("nightly") is not None:
                if cherrypy.request.params.get("nightly")=="true":
                    prereleaseRequested=True


        if userAgent is None or userAgent!="ESP8266-http-Update":
            cherrypy.response.status=403
            logger.error("HTTPUpdate - Wrong User Agent")
            return "Error - Wrong User Agent"

        if currentDeviceVer is None:
            cherrypy.response.status=406
            logger.error("HTTPUpdate - Error - no version")
            return "Error - no version"

        # carve that up
        hardware = currentDeviceVer.split("|")
        if len(hardware)!=2:
            # not expected
            cherrypy.response.status=406
            logger.warning("HTTPUpdate - Error - malformed hardware|version %s",currentDeviceVer)
            return "Error - malformed hardware|version"

        # then carve it up
        deviceVersion=self.crackVersion(hardware[1])

        # check for prerelease request
        logger.debug("nightly requested %s",prereleaseRequested)


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
        asset_dir="nightly" if prereleaseRequested==True else "releases"

        Node = self._config["manifest"][asset_dir]

        if "tag_name" not in Node or not self.vgreater(deviceVersion,self.crackVersion(Node["tag_name"])):
            cherrypy.response.status=304
            logger.info("No upgrade available for %s",deviceVersion)
            return "No upgrade"


        # now we have to carve the hardware
        lookfor=hardware[0]+"-.*\\."+fileTail+"$"
        r=re.compile(lookfor)
        newlist = list(filter(r.match, Node["files"])) # Read Note

        #should only be one candidate
        if len(newlist)!=1:
            cherrypy.response.status=500
            logger.warning("HTTPUpdate - No candidate")
            return "No candidate"

        # TODO remove this
        # logger.info("Would have served them a file {}".format(newlist[0]))

        # cherrypy.response.status=500
        # logger.warning("HTTPUpdate - No candidate")
        # return "No candidate"




        macAddress = cherrypy.request.headers.get('X-Esp8266-Sta-Mac')
        #logger.info(cherrypy.request.headers)
        logger.info("Heard from %s - %s",currentDeviceVer, macAddress) 

        name= os.path.join(os.path.join(DATA_STEM,asset_dir),newlist[0])

        logger.info("returning %s",name)

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

    main_running=True

    def signal_handler(sig, frame):

        global main_running
        logger.warning("Detected SIGINT")
        # stop my thread
        myrels.stopPoller()
        cherrypy.engine.exit()
        main_running=False

    signal.signal(signal.SIGINT, signal_handler)


    try:

        cherrypy.config.update({'server.socket_port': myrels.port()})
        cherrypy.config.update({'server.socket_host' : '0.0.0.0'})

        cherrypy.quickstart(myrels)

        while main_running:
            time.sleep(2)

    except Exception as e:

        logger.error(e)

    logger.warning("exiting")

    myrels.stopPoller()

    logger.critical("Exiting main")

