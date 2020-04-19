from flask import Flask, request
import json
import threading
import os
import requests
import shutil
import tarfile
import time
import signal
import logging

app = Flask(__name__)

@app.route('/')
def index():
    return 'Hello world'


CONFIG_FILE="config.json"
LOG_FILE="server.log"




class RepoReleases:

    def __init__(self, owner, repo):

        self._repo=repo
        self._owner=owner

        self._releases=[]
        self._prereleases=[]

        self._running=False
        self._stop=False

        self._poller = threading.Thread(target=self.fetchAssetsTimed, args=(5,))

        self._poller.start()

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



    def stopPoller(self):

        if self._running==True:

            logging.debug("requesting poll stop ...")
            self._stop=True
            
            while self._running==True:
                pass

            logging.debug("poll stopped!")

    # threaded function        
    def fetchAssetsTimed(self, timeout):

        logging.critical("fetchAssetsTimed started ...")

        self._running=True

        self._lastPoll=None

        while not self._stop:

            if self._lastPoll is None or ((time.time()-self._lastPoll)>timeout*60):

                logging.debug("Doing a poll")

                self.downloadLatestAssets()

                self._lastPoll=time.time()

            time.sleep(5)

        self._running=False

        logging.critical("fetchAssetsTimed stopping ...")

            



os.remove(LOG_FILE)
logging.basicConfig(filename=LOG_FILE,level=logging.DEBUG)



# entry point
if __name__ == '__main__':

    myrels=RepoReleases("barneyman","ESP8266-Light-Switch")

    def signal_handler(sig, frame):

        logging.info("Detected SIGINT")
        # stop my thread
        myrels.stopPoller()

        # stop flask
        func = request.environ.get('werkzeug.server.shutdown')
        if func is None:
            raise RuntimeError('Not running with the Werkzeug Server')
        else:
            func()

    # handle sigint
    # signal.signal(signal.SIGINT, signal_handler)



    try:
        #app.run(debug=True, host='0.0.0.0', port=8084)
        while True:
            pass

    except Exception as e:

        logging.error(e)


    myrels.stopPoller()

    logging.critical("Exiting main")

