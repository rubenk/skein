#!/usr/bin/python

import os
import sys
import time
import shutil
import argparse
import subprocess

# import our basic settings
from goose_settings import *

# import the rpm parsing stuff
import rpm

import pyskein
# import github creation 
#from create_gh_repo import *

# import git repo creation
#from create_git_repo import *

def main():

    ps = pyskein.PySkein()

    p = argparse.ArgumentParser(
            description=u"Imports all src.rpms into git and lookaside cache",
        )

    sp = p.add_subparsers()

    p_upload = sp.add_parser("sources", help=u"upload an srpm archive")
    p_upload.add_argument('srpm', help=u"path to archive")
    p_upload.add_argument('--new', action="store_true", help=u"new sources will replace old source")
    p_upload.set_defaults(func=ps.do_sources)

    p_import = sp.add_parser("import", help=u"import srpm(s)")
    #p_import.add_argument("path", nargs='+', help=u"path to srpm. If dir given, will import all srpms")
    p_import.add_argument("path", help=u"path to srpm. If dir given, will import all srpms")
    p_import.set_defaults(func=ps.do_import)

    args = p.parse_args()
#    print "Args: %s" % str(args)

    args.func(args)

if __name__ == "__main__":
    from skein import main
    raise SystemExit(main())
