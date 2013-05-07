from xdm.logger import *
from xdm import common
from xdm.classes import *
from xdm.plugins import Indexer
import datetime
import os
import json
from xdm.jsonHelper import MyEncoder
import threading
from cherrypy._cprequest import Hook


def runSearcher():
    log("running searcher")
    for mtm in common.PM.MTM:
        for ele in mtm.getDownloadableElements():
            if ele.status == common.FAILED and common.SYSTEM.c.again_on_fail:
                ele.status = common.WANTED
            elif ele.status != common.WANTED:
                continue
            #TODO: find a standart way for a release date maybe just add it :/
            """elif ele.release_date and ele.release_date > datetime.datetime.now(): # is the release date in the future
                continue"""
            log("Looking for %s" % ele)
            searchElement(ele)


def notify(element):
    for notifier in common.PM.N:
        createGenericEvent(element, 'Notifier', 'Sending notification with %s on status %s' % (notifier, element.status))
        if notifier.c.on_snatch and element.status == common.SNATCHED:
            notifier.sendMessage("%s has been snatched" % element.getName(), element)
        if notifier.c.on_complete and element.status in (common.COMPLETED, common.DOWNLOADED, common.PP_FAIL):
            notifier.sendMessage("%s is now %s" % (element, element.status), element)


def createGenericEvent(game, event_type, event_msg):
    h = History()
    h.game = game
    h.event = event_type
    h.obj_id = 0
    h.obj_class = 'GenericEvent'
    h.obj_type = 'Event'
    h.old_obj = json.dumps(game, cls=MyEncoder)
    h.new_obj = json.dumps({'_data': {'msg': event_msg}})
    h.save()


def commentOnDownload(download):
    for indexer in common.PM.I:
        if indexer.type != download.indexer or indexer.instance != download.indexer_instance:
            continue
        if indexer.c.comment_on_download and download.status == common.FAILED:
            indexer.commentOnDownload('Gamez snatched this but it failed to download (automtic notice)', download)
        if indexer.c.comment_on_download and download.status in (common.COMPLETED, common.DOWNLOADED, common.PP_FAIL):
            indexer.commentOnDownload('Gamez snatched this and it downloaded successfully (automtic notice)', download)


def searchElement(ele):
    didSearch = False
    for indexer in common.PM.getIndexers(runFor=ele.manager):
        createGenericEvent(ele, 'Search', 'Searching %s on %s' % (ele, indexer))
        downloads = indexer.searchForElement(ele) #intensiv
        didSearch = True

        #downloads = _filterBadDownloads(blacklist, whitelist, downloads)
        downloads = _filterBadDownloads(downloads)
        if downloads:
            return snatchOne(ele, downloads)
        else:
            log.info("We filtered all downloads out for %s" % ele)
    if not didSearch:
        log.warning("No Indexer active/available for %s" % ele.manager)
    return ele.status


# in a way we dont need ele here since each download holds a ref to each ele ... but it is easier to read
def snatchOne(ele, downloads):
    for downloader in common.PM.getDownloaders():
        triedSnatch = False
        for download in downloads:
            if not download.type in downloader.types:
                continue
            createGenericEvent(ele, 'Snatch', 'Trying to snatch %s with %s' % (download.name, downloader))
            log.info('Trying to snatch %s with %s' % (download.name, downloader))
            if downloader.addDownload(download):
                ele.status = common.SNATCHED
                ele.save()
                download.status = common.SNATCHED
                download.save()
                notify(ele)
                return ele.status #exit on first success
            triedSnatch = True
        if triedSnatch and downloads:
            log.warning("No Downloaders active/available for %s (or they all failed)" % download.type)
        elif not downloads:
            log.info("No downloads found for %s" % download.element)
    return ele.status


def _filterBadDownloads(downloads):
    clean = []
    for download in downloads:
        old_download = None
        try:
            old_download = Download.get(Download.url == download.url)
        except Download.DoesNotExist:
            #no download with that url found
            pass

        if not old_download:
            log("Saving the new download we found %s" % download)
            download.status = common.UNKNOWN
            download.save()
        else:
            try:
                Element.get(Element.id == download.element.id)
            except Element.DoesNotExist:
                log.warning("The element for the download(%s) does not exist any more deleting the old one but taking the status from the old one" % download.id)
                download.status = old_download.status
                old_download.delete_instance()
                download.save()
                old_download = download
            if old_download.status in (common.FAILED, common.DOWNLOADED):
                log.info("Found a Download(%s) with the same url and it failed or we downloaded it already. Skipping..." % download)
                continue
            if old_download.status == common.SNATCHED:
                if common.SYSTEM.c.resnatch_same:
                    continue
                log.info("Found a Download(%s) with the same url and we snatched it already. I'l get it again..." % download)
            download = old_download

        for curFilterPlugin in common.PM.getFilters(hook=common.FOUNDDOWNLOADS, runFor=download.element.manager):
            acceptence, string = curFilterPlugin.compare(element=download.element, download=download)
            if not acceptence:
                log.info('%s did not like %s' % (curFilterPlugin, download))
                break
        else:
            clean.append(download)
    return clean


def runChecker():
    elements = list(Element.select().execute())
    for checker in common.PM.D:
        for element in elements:
            if not element.status == common.SNATCHED:
                continue
            log("Checking status for %s" % element)
            status, download, path = checker.getGameStaus(element)
            log("%s gave back status %s for %s on download %s" % (checker, status, element, download))
            if status == common.DOWNLOADED:
                element.status = common.DOWNLOADED
                if download.id:
                    download.status = common.DOWNLOADED
                    download.save()
                ppElement(element, download, path)
                notify(element)
                if download.id:
                    commentOnDownload(download)
            elif status == common.SNATCHED:
                element.status = common.SNATCHED
                element.save()
                download.status = common.SNATCHED
                download.save()
            elif status == common.FAILED:
                download.status = common.FAILED
                download.save()
                if common.SYSTEM.c.again_on_fail:
                    element.status = common.WANTED
                    searchElement(element)
                else:
                    element.status = common.FAILED


def ppElement(element, download, path):
    pp_try = False
    for pp in common.PM.getPostProcessors(runFor=element.manager):
        createGenericEvent(element, 'PostProcess', 'Starting PP with %s' % pp)
        log('Starting PP on %s with %s at %s' % (element, pp, path))
        if pp.ppPath(element, path):
            element.status = common.COMPLETED
            element.save()
            download.status = common.COMPLETED
            download.save()
            return True
        pp_try = True
    if pp_try:
        element.status = common.PP_FAIL # tried to pp but fail
        download.status = common.PP_FAIL
        download.save()
    return False


def updateElement(element, force=False):
    for p in common.PM.getProvider(runFor=element.manager):
        #TODO: make sure we use the updated element after one provider is done
        pID = element.getField('id', p.tag)
        if not pID:
            log.info('we dont have this element(%s) on provider(%s) yet. we will search for it' % (element, p))
            #TODO search element by name or with help of xem ... yeah wishful thinking

        new_e = p.getElement(pID)
        createGenericEvent(element, 'Update', 'Serching for update on %s' % p)
        if new_e:
            log.info("%s returned an element" % p)
        else:
            log.info("%s returned NO element" % p)
        if new_e and new_e != element:
            log.info("Found new version of %s" % element)
            new_e.id = element.id
            new_e.status = element.status
            #delete old version
            element.deleteWithChildren()
            new_e.manager.makeReal(element)
            new_e.save()
            new_e.downloadImages()


def removeTempElements():
    def action():
        log.info("Removeing temp elements")
        for temp in Element.select().where(Element.status == common.TEMP):
            temp.delete_instance(silent=True)

    timer = threading.Timer(1, action)
    timer.start()
    
    