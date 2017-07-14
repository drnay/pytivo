import os
import sys
import threading
import time
import unicodedata
import logging
import urllib.request
import urllib.parse
import urllib.error

from functools import cmp_to_key
from operator import attrgetter
from lrucache import LRUCache

if os.path.sep == '/':
    quote = urllib.parse.quote
    unquote = urllib.parse.unquote_plus
else:
    quote = lambda x: urllib.parse.quote(x.replace(os.path.sep, '/'))
    unquote = lambda x: os.path.normpath(urllib.parse.unquote_plus(x))

class Error:
    CONTENT_TYPE = 'text/html'

def GetPlugin(name):
    """
    Get the plugin instance for a type with the given name
    """
    try:
        module_name = '.'.join(['plugins', name, name])
        module = __import__(module_name, globals(), locals(), name)
        plugin = getattr(module, module.CLASS_NAME)()
        return plugin
    except ImportError as e:
        logger = logging.getLogger('pyTivo.plugin')
        logger.error('Error no %s plugin exists. Check the type setting for your share.', name)
        logger.debug('Exception: %s', e)
        return Error

class Plugin(object):
    """
    Plugin derived classes are singletons. Calling the constructor
    always returns the same instance.
    see https://pytivo.sourceforge.io/wiki/index.php/Plugin_Guide

    Derived classes must not have an __init__ method, instead create
    an init method (so it is only called when the initial instance is
    created by __new__).
    """

    random_lock = threading.Lock()

    CONTENT_TYPE = ''

    recurse_cache = LRUCache(5)
    dir_cache = LRUCache(10)

    def __new__(cls, *args, **kwds):
        it = cls.__dict__.get('__it__')
        if it is not None:
            return it
        cls.__it__ = it = object.__new__(cls)
        it.init(*args, **kwds)
        return it

    def init(self):
        self.logger = logging.getLogger('pyTivo.plugin')

    def send_file(self, handler, path, query):
        handler.send_content_file(path)

    def get_local_base_path(self, handler, query):
        return os.path.normpath(handler.container['path'])

    def get_local_path(self, handler, query):

        subcname = query['Container'][0]

        path = self.get_local_base_path(handler, query)
        for folder in subcname.split('/')[1:]:
            if folder == '..':
                return False
            path = os.path.join(path, folder)
        return path

    def item_count(self, handler, query, cname, files, last_start=0):
        """
        Return only the desired portion of the list, as specified by
        ItemCount, AnchorItem and AnchorOffset. 'files' is either a
        list of strings, OR a list of objects with a 'name' attribute.
        """

        def no_anchor(handler, anchor):
            handler.server.logger.warning('Anchor not found: ' + anchor)

        totalFiles = len(files)
        index = 0

        if totalFiles and 'ItemCount' in query:
            count = int(query['ItemCount'][0])

            if 'AnchorItem' in query:
                bs = '/TiVoConnect?Command=QueryContainer&Container='
                local_base_path = self.get_local_base_path(handler, query)

                anchor = query['AnchorItem'][0]
                if anchor.startswith(bs):
                    anchor = anchor.replace(bs, '/', 1)
                anchor = unquote(anchor)
                anchor = anchor.replace(os.path.sep + cname, local_base_path, 1)
                if not '://' in anchor:
                    anchor = os.path.normpath(anchor)

                if isinstance(files[0], str):
                    filenames = files
                else:
                    filenames = [x.name for x in files]
                try:
                    index = filenames.index(anchor, last_start)
                except ValueError:
                    if last_start:
                        try:
                            index = filenames.index(anchor, 0, last_start)
                        except ValueError:
                            no_anchor(handler, anchor)
                    else:
                        no_anchor(handler, anchor) # just use index = 0

                if count > 0:
                    index += 1

                if 'AnchorOffset' in query:
                    index += int(query['AnchorOffset'][0])

            if count < 0:
                index = (index + count) % len(files)
                count = -count
            files = files[index:index + count]

        return files, totalFiles, index

    def get_files(self, handler, query, filterFunction=None,
                  force_alpha=False, allow_recurse=True):

        class FileData:
            def __init__(self, name, isdir):
                self.name = name
                self.isdir = isdir
                st = os.stat(name)
                self.mdate = st.st_mtime
                self.size = st.st_size

            def __repr__(self):
                return "FileData({}, {})".format(self.name, self.isdir)

        class SortList:
            def __init__(self, files):
                self.files = files
                self.unsorted = True
                self.sortby = None
                self.last_start = 0

        def build_recursive_list(path, recurse=True):
            files = []
            try:
                for f in os.listdir(path):
                    if f.startswith('.'):
                        continue
                    f = os.path.join(path, f)
                    isdir = os.path.isdir(f)
                    if sys.platform == 'darwin':
                        f = unicodedata.normalize('NFC', f)
                    if recurse and isdir:
                        files.extend(build_recursive_list(f))
                    else:
                        if not filterFunction or filterFunction(f, file_type):
                            files.append(FileData(f, isdir))
            except:
                pass
            return files

        path = self.get_local_path(handler, query)

        file_type = query.get('Filter', [''])[0]

        recurse = allow_recurse and query.get('Recurse', ['No'])[0] == 'Yes'

        filelist = []
        rc = self.recurse_cache
        dc = self.dir_cache
        if recurse:
            if path in rc and rc.mtime(path) + 300 >= time.time():
                filelist = rc[path]
        else:
            updated = os.path.getmtime(path)
            if path in dc and dc.mtime(path) >= updated:
                filelist = dc[path]
            for p in rc:
                if path.startswith(p) and rc.mtime(p) < updated:
                    del rc[p]

        if not filelist:
            filelist = SortList(build_recursive_list(path, recurse))

            if recurse:
                rc[path] = filelist
            else:
                dc[path] = filelist

        def dir_cmp(x, y):
            if x.isdir == y.isdir:
                if x.name < y.name:
                    return -1
                if x.name == y.name:
                    return 0
                return 1
            else:
                return y.isdir - x.isdir

        sortby = query.get('SortOrder', ['Normal'])[0]
        if filelist.unsorted or filelist.sortby != sortby:
            if force_alpha:
                filelist.files.sort(key=cmp_to_key(dir_cmp))
            elif sortby == '!CaptureDate':
                filelist.files.sort(key=attrgetter('mdate'), reverse=True)
            else:
                filelist.files.sort(key=attrgetter('name'))

            filelist.sortby = sortby
            filelist.unsorted = False

        files = filelist.files[:]

        # Trim the list
        files, total, start = self.item_count(handler, query, handler.cname,
                                              files, filelist.last_start)
        if len(files) > 1:
            filelist.last_start = start
        return files, total, start
