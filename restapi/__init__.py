#-------------------------------------------------------------------------------
# Name:        restapi
# Purpose:     provides helper functions for Esri's ArcGIS REST API
#              -Designed for external usage
#
# Author:      Caleb Mackey
#
# Created:     10/29/2014
# Copyright:   (c) calebma 2014
# Licence:     BMI
#-------------------------------------------------------------------------------
import admin

# look for arcpy access, otherwise use open source version
# open source version may be faster.
##try:
##    import imp
##    imp.find_module('arcpy')
##    from arc_restapi import *
##    __opensource__ = False
##except ImportError:
##    from open_restapi import *
##    __opensource__ = True

from common_types import *

# package info
__author__ = 'Caleb Mackey'
__organization__ = 'Bolton & Menk, Inc.'
__author_email__ = 'calebma@bolton-menk.com'
__website__ = 'https://github.com/Bolton-and-Menk-GIS/restapi'
__version__ = '1.0'
__documentation__ = 'http://gis.bolton-menk.com/restapi-documentation/restapi-module.html'
__keywords__ = ['rest', 'arcgis-server', 'requests', 'http', 'administration', 'rest-services']
__description__ = 'Python API for working with ArcGIS REST API. This package has been designed to ' + \
    'work with arcpy or open source and does not require arcpy. It will try to use arcpy if available ' + \
    'for some data conversions, otherwise will use open source options. Also included is a subpackage ' + \
    'for administering ArcGIS Server Sites.'

def getHelp():
    """call this function to open help documentation in a new tab"""
    import webbrowser
    webbrowser.open_new_tab(__documentation__)
    return __documentation__
