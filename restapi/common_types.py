# look for arcpy access, otherwise use open source version
from __future__ import print_function

import sqlite3
import base64
import shutil
import contextlib
from .rest_utils import *
from .decorator import decorator
import sys
import warnings
from munch import munchify
from . import projections

import six
from six.moves import urllib, zip_longest

DEFAULT_REQUEST_FORMAT = JSON

__opensource__ = False

try:
    if str(os.environ.get('RESTAPI_USE_ARCPY')).upper() in ('FALSE', '0'):
        raise ImportError
    import arcpy
    from .arc_restapi import *
    has_arcpy = True

except Exception as e:
    print('arcpy import error: ', e)
    # using global is throwing a warning???
    setattr(sys.modules[PACKAGE_NAME], '__opensource__', True)
    __opensource__ = True
    warnings.warn('No Arcpy found, some limitations in functionality may apply.')
    # global DEFAULT_REQUEST_FORMAT 
    DEFAULT_REQUEST_FORMAT = GEOJSON
    from .open_restapi import *
    has_arcpy = False
    class Callable(object):
        def __call__(self, *args, **kwargs):
            raise NotImplementedError('No Access to arcpy!')

        def __getattr__(self, attr):
            """Recursively raise not implemented error for any calls to arcpy:

                arcpy.management.AddField(...)

            or:

                arcpy.AddField_management(...)
            """
            return Callable()

        def __repr__(self):
            return ''

    class ArcpyPlaceholder(object):
        def __getattr__(self, attr):
            return Callable()

        def __setattr__(self, attr, val):
            pass

        def __repr__(self):
            return ''

    arcpy = ArcpyPlaceholder()

    # datetime to date string
    def datetime_to_datestring(d):
        if d:
            if isinstance(d, datetime.datetime):
                return d.strftime('%Y%m%d')
            return mil_to_date(d).strftime('%Y%m%d')
        return d


USE_GEOMETRY_PASSTHROUGH = True #can be set to false to not use @geometry_passthrough

# extend feature to get geometry
def get_geometry_object(self):
    return Geometry(getattr(self, GEOMETRY))

Feature.getGeometry = get_geometry_object

@decorator
def geometry_passthrough(func, *args, **kwargs):
    """Decorator to return a single geometry if a single geometry was returned
        in a GeometryCollection(), otherwise returns the full GeometryCollection().
    
    Args:
        func: Function to decorate.
        *args: Args to pass into function.
        **kwargs: Keyword args to pass into function.
    """

    f = func(*args, **kwargs)
    gc = GeometryCollection(f)
    if gc.count == 1 and USE_GEOMETRY_PASSTHROUGH:
        return gc[0]
    else:
        return gc
    return f

def getFeatureExtent(in_features):
    """Gets the extent for a FeatureSet() or GeometryCollection(), must be convertible
    to a GeometryCollection().

    Arg:
        in_features: Input features (Feature|FeatureSet|GeometryCollection|json).
    
    Returns:
        An envelope json structure (extent).
    """

    if not isinstance(in_features, GeometryCollection):
        in_features = GeometryCollection(in_features)

    extents = [g.envelopeAsJSON() for g in iter(in_features)]
    full_extent = {SPATIAL_REFERENCE: extents[0].get(SPATIAL_REFERENCE)}
    for attr, op in {XMIN: min, YMIN: min, XMAX: max, YMAX: max}.iteritems():
        full_extent[attr] = op([e.get(attr) for e in extents])
    return munch.munchify(full_extent)

def unqualify_fields(fs):
    """Removes fully qualified field names from a feature set.

    Arg:
        fs: restapi.FeatureSet() object or JSON.
    """

    if not isinstance(fs, FeatureSet):
        fs = FeatureSet(fs)

    clean_fields = {}
    for f in fs.fields:
        if f:
            clean = f.name.split('.')[-1]
            clean_fields[f.name] = clean
            f.name = clean

    for i,feature in enumerate(fs.features):
        feature_copy = {}
        for f, val in six.iteritems(feature.attributes):
            feature_copy[clean_fields.get(f, f)] = val
        fs.features[i].attributes = munch.munchify(feature_copy)

def exportFeatureSet_arcpy(feature_set, out_fc, include_domains=False, qualified_fieldnames=False, append_features=True, **kwargs):
        """Exports FeatureSet (JSON result)  to shapefile or feature class.

        Args:
            feature_set: JSON response obtained from a query or FeatureSet() object.
            out_fc: Output feature class or shapefile.  If the output exists, 
                features are appended at the end.
                (unless append_features is set to False)
            include_domains: Optional boolean, if True, will manually create the 
                feature class and add domains to GDB if output is in a geodatabase.
                Defaults to False.
            qualified_fieldnames: Optional boolean, default is False, in situations 
                where there are table joins, there are qualified table names such as 
                ["table1.Field_from_tab1", "table2.Field_from_tab2"]. By setting 
                this to False, exported fields would be: 
                ["Field_from_tab1", "Field_from_tab2"].
            append_features: Optional boolean to append features if the output 
                features already exist.  Set to False to overwrite features.

        At minimum, feature set must contain these keys:
            [u'features', u'fields', u'spatialReference', u'geometryType']
        
        Returns:
            A feature class.
        """ 

        if __opensource__:
            return exportFeatureSet_os(feature_set, out_fc, **kwargs)

        out_fc = validate_name(out_fc)
        # validate features input (should be list or dict, preferably list)
        if not isinstance(feature_set, FeatureSet):
            feature_set = FeatureSet(feature_set)

        if not qualified_fieldnames:
            unqualify_fields(feature_set)

        # find workspace type and path
        ws, wsType = find_ws_type(out_fc)
        isShp = wsType == 'FileSystem'
        temp = time.strftime(r'in_memory\restapi_%Y%m%d%H%M%S') #if isShp else None
        original = out_fc
        gp = arcpy.geoprocessing._base.Geoprocessor()
        try:
            hasGeom = GEOMETRY in feature_set.features[0]
        except:
            print('could not check geometry!')
            hasGeom = False

        # try converting JSON features from arcpy, seems very fragile...
        exists = arcpy.Exists(original)
        if not exists or (exists and not append_features):
            if exists:
                arcpy.management.Delete(out_fc)
            if isShp:
                out_fc = temp
            try:
                try:
                    arcpy_fs = gp.fromEsriJson(feature_set.dumps(indent=None)) #arcpy.FeatureSet from JSON string
                    arcpy_fs.save(out_fc)
                except:
                    tmp = feature_set.dump(tmp_json_file(), indent=None)
                    arcpy.conversion.JSONToFeatures(tmp, out_fc) #this tool is very buggy :(

            except:
                # manually add records with insert cursor
                print('arcpy conversion failed, manually writing features...')
                create_empty_schema(feature_set, out_fc)
                append_feature_set(out_fc, feature_set, Cursor)

        else:

            # append rows
            try:
                try:
                    tmp = gp.fromEsriJson(feature_set.dumps(indent=None))
                except Exception as e:
                    print('from esri json exception: ', e)
                    tmp_fs = feature_set.dump(tmp_json_file(), indent=None)
                    arcpy.conversion.JSONToFeatures(tmp_fs, tmp)
                arcpy.management.Append(tmp, out_fc, 'NO_TEST')

            except Exception as e:
                print('arcpy conversion failed, manually appending features', e)
                append_feature_set(out_fc, feature_set, Cursor)

        # copy in_memory fc to shapefile
        if isShp and original != out_fc:
            arcpy.management.CopyFeatures(out_fc, original)
            if arcpy.Exists(temp):
                arcpy.management.Delete(temp)

        # add domains
        if include_domains and not isShp:
            add_domains_from_feature_set(out_fc, feature_set)

        if exists:
            print('Appended {} features to "{}"'.format(feature_set.count, original))
        else:
            print('Created: "{0}"'.format(original))
        return original


def exportFeatureSet_os(feature_set, out_fc, outSR=None, **kwargs):
        """Exports features (JSON result) to shapefile or feature class.

        Args:
            out_fc: Output feature class or shapefile.
            feature_set: JSON response (feature set) obtained from a query.
            outSR: Optional output spatial reference.  If none set, will default
                to SR of result_query feature set. Defaults to None.
        
        Returns:
            Feature class.
        """
        from . import shp_helper
        out_fc = validate_name(out_fc)
        # validate features input (should be list or dict, preferably list)
        if not isinstance(feature_set, (FeatureSet, GeoJSONFeatureSet)):
            feature_set = FeatureSet(feature_set)

        # make new shapefile
        fields = feature_set.fields
        this_sr = feature_set.getSR()
        date_fields = [f.name for f in fields if f.type == DATE_FIELD]
        if not outSR:
            outSR = this_sr
        else:
            if this_sr:
                if outSR != this_sr:
                    # do not change without reprojecting...
                    outSR = this_sr

        g_type = getattr(feature_set, GEOMETRY_TYPE)

        # add all fields
        w = shp_helper.ShpWriter(out_fc, G_DICT[g_type].upper())
        field_map = []
        for fld in fields:
            if fld.type not in [OID, SHAPE] + list(SKIP_FIELDS.keys()):
                if not any(['shape_' in fld.name.lower(),
                            'shape.' in fld.name.lower(),
                            '(shape)' in fld.name.lower(),
                            'objectid' in fld.name.lower(),
                            fld.name.lower() == 'fid']):

                    field_name = fld.name.split('.')[-1][:10]
                    field_type = SHP_FTYPES[fld.type]
                    field_length = str(fld.length) if hasattr(fld, 'length') else "50"
                    w.add_field(field_name, field_type, field_length)
                    field_map.append((fld.name, field_name))

        # search cursor to write rows
        s_fields = [fl for fl in fields if fl.name in [f[0] for f in field_map]]
        for feat in feature_set:
            # print(feat)
            row = [datetime_to_datestring(feat.get(field)) if field in date_fields else feat.get(field) for field in [f[0] for f in field_map]]
            w.add_row(feat.getGeometry().asShape(), *row)

        w.save()
        print('Created: "{0}"'.format(out_fc))

        # write projection file
        project(out_fc, outSR)
        return out_fc

if has_arcpy:
    exportFeatureSet = exportFeatureSet_arcpy

else:
    exportFeatureSet = exportFeatureSet_os

def exportGeometryCollection(gc, output, **kwargs):
    """Returns and exports a geometry collection to shapefile or feature class.

    Args:
        gc: GeometryCollection() object.
        output: Output data set (will be geometry only).
    
    Raises:
        ValueError: "Input is not a GeometryCollection!"
    """ 

    if isinstance(gc, Geometry):
        gc = GeometryCollection(gc)
    if not isinstance(gc, GeometryCollection):
        raise ValueError('Input is not a GeometryCollection!')

    fs_dict = {}
    fs_dict[SPATIAL_REFERENCE] = {WKID: gc.spatialReference}
    fs_dict[GEOMETRY_TYPE] = gc.geometryType
    fs_dict[DISPLAY_FIELD_NAME] = ''
    fs_dict[FIELD_ALIASES] = {OBJECTID: OBJECTID}
    fs_dict[FIELDS] = [{NAME: OBJECTID,
                        TYPE: OID,
                        ALIAS: OBJECTID}]
    fs_dict[FEATURES] = [{ATTRIBUTES: {OBJECTID:i+1}, GEOMETRY:ft.json} for i,ft in enumerate(gc)]

    fs = FeatureSet(fs_dict)
    return exportFeatureSet(fs, output, **kwargs)

def featureIterator(obj):
    if hasattr(obj, 'features'):
    	for ft in iter(obj.features):
    		yield Feature(ft)

FeatureSet.__iter__ = featureIterator

class Cursor(FeatureSet):
    """Class to handle Cursor object."""
    json = {}
    fieldOrder = []
    field_names = []

    class BaseRow(object):
        """Class to handle Row object.
        
        Attributes:
            feature: A feature JSON object.
            spatialReference: A spatial reference.
        """
        def __init__(self, feature, spatialReference):
            """Row object for Cursor.
            
            Args:
                feature: Features JSON object.
                spatialReference: A spatial reference.
            """

            self.feature = Feature(feature) if not isinstance(feature, Feature) else feature
            self.spatialReference = spatialReference

        def get(self, field):
            """Gets/returns an attribute by field name.

            Arg:
                field: Name of field for which to get the value.
            """

            return self.feature.attributes.get(field)

    def __init__(self, feature_set, fieldOrder=[]):
        """Cursor object for a feature set.
        
        Args:
            feature_set: Feature set as json or restapi.FeatureSet() object.
            fieldOrder: Optional, list of order of fields for cursor row returns. 
                To explicitly specify and OBJECTID field or Shape (geometry field), 
                you must use the field tokens 'OID@' and 'SHAPE@' respectively.
                Defaults to [].
        """

        if isinstance(feature_set, FeatureSet):
            feature_set = feature_set.json
        super(Cursor, self).__init__(feature_set)
        self.fieldOrder = self.__validateOrderBy(fieldOrder)

        cursor = self
        class Row(cursor.BaseRow):
            """Class to handle Row object."""

            @property
            def geometry(self):
                """Returns a restapi Geometry() object."""
                if GEOMETRY in self.feature.json:
                    gd = {k: v for k,v in six.iteritems(self.feature.geometry)}
                    if SPATIAL_REFERENCE not in gd:
                        gd[SPATIAL_REFERENCE] = cursor.spatialReference
                    return Geometry(gd)
                return None

            @property
            def oid(self):
                """Returns the OID for row."""
                if cursor.OIDFieldName:
                    return self.get(cursor.OIDFieldName)
                return None

            @property
            def values(self):
                """Returns values as tuple."""
                # fix date format in milliseconds to datetime.datetime()
                vals = []
                for field in cursor.field_names:
                    if field in cursor.date_fields and self.get(field):
                        vals.append(mil_to_date(self.get(field)))
                    elif field in cursor.long_fields and self.get(field):
                        vals.append(int(self.get(field)))
                    else:
                        if field == OID_TOKEN:
                            vals.append(self.oid)
                        elif field == SHAPE_TOKEN:
                            if self.geometry:
                                vals.append(self.geometry.asShape())
                            else:
                                vals.append(None) #null Geometry
                        else:
                            vals.append(self.get(field))

                return tuple(vals)

            def __getitem__(self, i):
                """Allows for getting a field value by index.
                
                Arg:
                    i: Index to get value from.
                """

                return self.values[i]

        # expose Row object
        self.__Row = Row

    @property
    def date_fields(self):
        """Gets the names of any date fields within feature set."""
        return [f.name for f in self.fields if f.type == DATE_FIELD]

    @property
    def long_fields(self):
        """Field names of type Long Integer, need to know this for use with
            arcpy.da.InsertCursor() as the values need to be cast to long.
        
        Returns:
            The names of the Long Integer fields.
        """
        return [f.name for f in self.fields if f.type == LONG_FIELD]

    @property
    def field_names(self):
        """Gets and returns the field names for feature set."""
        names = []
        for f in self.fieldOrder:
            if f == OID_TOKEN and self.OIDFieldName:
                names.append(self.OIDFieldName)
            elif f == SHAPE_TOKEN and self.ShapeFieldName:
                names.append(self.ShapeFieldName)
            else:
                names.append(f)
        return names

    def get_rows(self):
        """Returns row objects."""
        for feature in self.features:
            yield self._createRow(feature, self.spatialReference)

    def rows(self):
        """Returns Cursor.rows() as generator."""
        for feature in self.features:
            yield self._createRow(feature, self.spatialReference).values

    def getRow(self, index):
        """Returns row object at index."""
        return self._createRow(self.features[index], self.spatialReference)

    def _toJson(self, row):
        """Casts row to JSON."""
        if isinstance(row, (list, tuple)):
            ft = {ATTRIBUTES: {}}
            for i,f in enumerate(self.field_names):
                if f != self.ShapeFieldName and f.upper() != SHAPE_TOKEN:
                    val = row[i]
                    if f in self.date_fields:
                        ft[ATTRIBUTES][f] = date_to_mil(val) if isinstance(val, datetime.datetime) else val
                    elif f in self.long_fields:
                        ft[ATTRIBUTES][f] = int(val) if val is not None else val
                    else:
                        ft[ATTRIBUTES][f] = val
                else:
                    geom = row[i]
                    if isinstance(geom, Geometry):
                        ft[GEOMETRY] = {k:v for k,v in six.iteritems(geom.json) if k != SPATIAL_REFERENCE}
                    else:
                        ft[GEOMETRY] = {k:v for k,v in six.iteritems(Geometry(geom).json) if k != SPATIAL_REFERENCE}
            return Feature(ft)
        elif isinstance(row, self.BaseRow):
            return row.feature
        elif isinstance(row, Feature):
            return row
        elif isinstance(row, dict):
            return Feature(row)

    def __iter__(self):
        """Returns Cursor.rows()."""
        return self.rows()

    def _createRow(self, feature, spatialReference):
        """Creates a row based off of the feature and spatial reference."""
        return self.__Row(feature, spatialReference)

    def __validateOrderBy(self, fields):
        """Fixes "fieldOrder" input fields, accepts esri field tokens too ("SHAPE@", "OID@").

        Arg:
            fields: List or comma delimited field list.
        
        Returns:
            The list of the fields.
        """

        if not fields or fields == '*':
            fields = [f.name for f in self.fields]
        if isinstance(fields, six.string_types):
            fields = fields.split(',')
        for i,f in enumerate(fields):
            if '@' in f:
                fields[i] = f.upper()
            if hasattr(self, 'ShapeFieldName') and f == self.ShapeFieldName:
                fields[i] = SHAPE_TOKEN
            if hasattr(self, 'OIDFieldName') and f == self.OIDFieldName:
                fields[i] = OID_TOKEN

        return fields

    def __repr__(self):
        return object.__repr__(self)

class JsonReplica(JsonGetter):
    """Represents a JSON replica.
    
    Attributes:
        json: JSON object.
    """

    def __init__(self, in_json):
        """Creates a JSON form input JSON."""
        self.json = munch.munchify(in_json)
        super(self.__class__, self).__init__()

class SQLiteReplica(sqlite3.Connection):
    """Represents a replica stored as a SQLite database."""
    def __init__(self, path):
        """Represents a replica stored as a SQLite database, this should ALWAYS
        be used with a context manager.  For example:

            with SQLiteReplica(r'C:\TEMP\replica.geodatabase') as con:
                print(con.list_tables())
                # do other stuff

        Arg:
            path: Full path to .geodatabase file (SQLite database).
        """

        self.db = path
        super(SQLiteReplica, self).__init__(self.db)
        self.isClosed = False

    @contextlib.contextmanager
    def execute(self, sql):
        """Executes an SQL query.  This method must be used via a "with" statement
        to ensure the cursor connection is closed.

        Arg:
            sql: SQL statement to use.

        >>> with restapi.SQLiteReplica(r'C:\Temp\test.geodatabase') as db:
        >>>     # now do a cursor using with statement
        >>>     with db.execute('SELECT * FROM Some_Table') as cur:
        >>>         for row in cur.fetchall():
        >>>             print(row)
        """

        cursor = self.cursor()
        try:
            yield cursor.execute(sql)
        finally:
            cursor.close()

    def list_tables(self, filter_esri=True):
        """Returns a list of tables found within sqlite table.

        Arg:
            filter_esri -- Optional boolean, filters out all the esri specific 
                tables (GDB_*, ST_*), default is True. If False, all tables will be listed.
        """

        with self.execute("select name from sqlite_master where type = 'table'") as cursor:
            tables = cursor.fetchall()
        if filter_esri:
            return [t[0] for t in tables if not any([t[0].startswith('st_'),
                                                     t[0].startswith('GDB_'),
                                                     t[0].startswith('sqlite_')])]
        else:
            return [t[0] for t in tables]

    def list_fields(self, table_name):
        """Lists fields within a table, returns a list of tuples with the following 
            attributes:

        cid         name        type        notnull     dflt_value  pk
        ----------  ----------  ----------  ----------  ----------  ----------
        0           id          integer     99                      1
        1           name                    0                       0

        Arg:
            table_name: Name of table to get field list from.
        """

        with self.execute('PRAGMA table_info({})'.format(table_name)) as cur:
            return cur.fetchall()

    def exportToGDB(self, out_gdb_path):
        """Exports the sqlite database (.geodatabase file) to a File Geodatabase, 
            requires access to arcpy. Warning:  All cursor connections must be 
            closed before running this operation!  If there are open cursors, 
            this can lock down the database.

        Arg:
            out_gdb_path: Full path to new file geodatabase. 
                (ex: r"C:\Temp\replica.gdb").
        """

        if not has_arcpy:
            raise NotImplementedError('no access to arcpy!')
        if not hasattr(arcpy.conversion, COPY_RUNTIME_GDB_TO_FILE_GDB):
            raise NotImplementedError('arcpy.conversion.{} tool not available!'.format(COPY_RUNTIME_GDB_TO_FILE_GDB))
        self.cur.close()
        if os.path.isdir(out_gdb_path):
            out_gdb_path = os.path.join(out_gdb_path, self._fileName)
        if not out_gdb_path.endswith('.gdb'):
            out_gdb_path = os.path.splitext(out_gdb_path)[0] + '.gdb'
        return arcpy.conversion.CopyRuntimeGdbToFileGdb(self.db, out_gdb_path).getOutput(0)

    def __safe_cleanup(self):
        """Closes connection and removes temporary .geodatabase file"""
        try:
            self.close()
            self.isClosed = True
            if TEMP_DIR in self.db:
                os.remove(self.db)
                self.db = None
                print('Cleaned up temporary sqlite database')
        except:
            pass

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.__safe_cleanup()

    def __del__(self):
        self.__safe_cleanup()

class ArcServer(RESTEndpoint):
    """Class to handle ArcGIS Server Connection.
    
    Attributes:
        service_cache: List of service cache.
    """

    def __init__(self, url, usr='', pw='', token='', proxy=None, referer=None):
        super(ArcServer, self).__init__(url, usr, pw, token, proxy, referer)
        self.service_cache = []

    def getService(self, name_or_wildcard):
        """Method to return Service Object (MapService, FeatureService, GPService, etc).
        This method supports wildcards.

        Arg:
            name_or_wildcard: Service name or wildcard used to grab service name.
                (ex: "moun_webmap_rest/mapserver" or "*moun*mapserver")
        
        Raises:
            NotImplementedError: 'restapi does not support "{}" services!'
        """
        full_path = self.get_service_url(name_or_wildcard)
        if full_path:
            extension = full_path.split('/')[-1]
            if extension == 'MapServer':
                return MapService(full_path, token=self.token)
            elif extension == 'FeatureServer':
                 return FeatureService(full_path, token=self.token)
            elif extension == 'GPServer':
                 return GPService(full_path, token=self.token)
            elif extension == 'ImageServer':
                 return ImageService(full_path, token=self.token)
            elif extension == 'GeocodeServer':
                return Geocoder(full_path, token=self.token)
            else:
                raise NotImplementedError('restapi does not support "{}" services!')

    @property
    def mapServices(self):
        """List of all MapServer objects."""
        if not self.service_cache:
            self.service_cache = self.list_services()
        return [s for s in self.service_cache if s.endswith('MapServer')]

    @property
    def featureServices(self):
        """List of all FeatureServer objects."""
        if not self.service_cache:
            self.service_cache = self.list_services()
        return [s for s in self.service_cache if s.endswith('FeatureServer')]

    @property
    def imageServices(self):
        """List of all ImageServer objects."""
        if not self.service_cache:
            self.service_cache = self.list_services()
        return [s for s in self.service_cache if s.endswith('ImageServer')]

    @property
    def gpServices(self):
        """List of all GPServer objects."""
        if not self.service_cache:
            self.service_cache = self.list_services()
        return [s for s in self.service_cache if s.endswith('GPServer')]

    def folder(self, name):
        """Returns a restapi.Folder() object.

        Arg:
            name: Name of folder.
        """
        return Folder('/'.join([self.ur, name]), token=self.token)

    def list_services(self, filterer=True):
        """Returns a list of all services."""
        return list(self.iter_services(filterer))

    def iter_services(self, token='', filterer=True):
        """Returns a generator for all services

        Arg:
            token: Optional token to handle security (only required if security is enabled).
        """
        self.service_cache = []
        for s in self.services:
            full_service_url = '/'.join([self.url, s[NAME], s[TYPE]])
            self.service_cache.append(full_service_url)
            yield full_service_url

        for s in self.folders:
            new = '/'.join([self.url, s])
            resp = self.request(new)
            for serv in resp[SERVICES]:
                full_service_url =  '/'.join([self.url, serv[NAME], serv[TYPE]])
                self.service_cache.append(full_service_url)
                yield full_service_url

    def get_service_url(self, wildcard='*', _list=False):
        """Method to return a service url.

        Args:
            wildcard: Wildcard used to grab service name (ex "moun*featureserver").
            _list: Default is false.  If true, will return a list of all services
                matching the wildcard.  If false, first match is returned.
        """

        if not self.service_cache:
            self.list_services()
        if '*' in wildcard:
            if wildcard == '*':
                return self.service_cache[0]
            else:
                if _list:
                    return [s for s in self.service_cache if fnmatch.fnmatch(s, wildcard)]
            for s in self.service_cache:
                if fnmatch.fnmatch(s, wildcard):
                    return s
        else:
            if _list:
                return [s for s in self.service_cache if wildcard.lower() in s.lower()]
            for s in self.service_cache:
                if wildcard.lower() in s.lower():
                    return s
        print('"{0}" not found in services'.format(wildcard))
        return ''

    def get_folders(self):
        """Method to get and return folder objects."""
        folder_objects = []
        for folder in self.folders:
            folder_url = '/'.join([self.url, folder])
            folder_objects.append(Folder(folder_url, self.token))
        return folder_objects

    def walk(self):
        """Method to walk through ArcGIS REST Services. ArcGIS Server only 
            supports single folder heiarchy, meaning that there cannot be 
            subdirectories within folders.

        Will return tuple of the root folder and services from the topdown.
        (root, services) example:

        ags = restapi.ArcServer(url, username, password)
        for root, folders, services in ags.walk():
            print(root)
            print(services)
            print('\n\n')
        """
        self.service_cache = []
        services = []
        for s in self.services:
            qualified_service = '/'.join([s[NAME], s[TYPE]])
            full_service_url = '/'.join([self.url, qualified_service])
            services.append(qualified_service)
            self.service_cache.append(full_service_url)
        yield (self.url, services)

        for f in self.folders:
            new = '/'.join([self.url, f])
            endpt = self.request(new)
            services = []
            for serv in endpt[SERVICES]:
                qualified_service = '/'.join([serv[NAME], serv[TYPE]])
                full_service_url = '/'.join([self.url, qualified_service])
                services.append(qualified_service)
                self.service_cache.append(full_service_url)
            yield (f, services)

    def __iter__(self):
        """Returns an generator for services."""
        return self.list_services()

    def __len__(self):
        """Returns number of services."""
        return len(self.service_cache)

    def __repr__(self):
        parsed = urllib.parse.urlparse(self.url)
        try:
            instance = parsed.path.split('/')[1]
        except IndexError:
            instance = '?'
        return '<ArcServer: "{}" ("{}")>'.format(parsed.netloc, instance)

class Portal(RESTEndpoint):
    """Class that handles the Portal
    
    Attributes:
        self.url: URL for site.
    """
    _elevated_token = None

    def __init__(self, url, usr='', pw='', token='', proxy=None, referer=None, **kwargs):
        """Gets login credentials for portal.
        
        Args:
            url: The URL.
            usr: Username to login with. Defaults to ''.
            pw: Password to login with. Defaults to ''.
            token: Token for the URL. Defaults to ''.
            proxy: Optional arg for proxy. Defaults to None.
            referer: Optional, defaults to None.
        """

        url = get_portal_base(url) + '/rest/portals/self'
        super(Portal, self).__init__(url, usr, pw, token, proxy, referer, **kwargs)
        service_url = self.json.get('helperServices', {}).get('printTask', {}).get('url', '').split('/Utilities')[0]

    @property
    def portalUrl(self):
        return get_portal_base(self.url)

    @property
    def servers(self):
        servers_url = get_portal_base(self.url).split('/sharing')[0] + '/portaladmin/federation/servers'
        serversResp = requests.get(servers_url, {TOKEN: self.token.token, F: JSON}, verify=False).json()
        return [ArcServer(s.get('url') + '/rest/services', token=self.token) for s in serversResp.get('servers', [])]


    # @property
    # def services_url(self):
    #     return self.portalUrl + '/servers'

    def getItem(self, itemId):
        item_url = self.portalUrl + '/rest/content/items/{}'.format(itemId)
        item = self.request(item_url, {TOKEN: str(self.token)})

        return item

    def fromItem(self, item):
        if item.type  == 'Feature Service':
            services_base = item.url.split('/rest/services/')[0] + '/rest/services'
            elevated_token = ID_MANAGER.tokens.get(services_base)
            if not elevated_token or not self._elevated_token:
                token = generate_elevated_portal_token(item.url, self.token)
                self._elevated_token = token

            return FeatureService(item.url)

    def __repr__(self):
        return '<{}: "{}">'.format(self.__class__.__name__, self.portalHostname)


class MapServiceLayer(RESTEndpoint, SpatialReferenceMixin, FieldsMixin):
    """Class to handle advanced layer properties."""

    def _fix_fields(self, fields):
        """Fixes input fields, accepts esri field tokens too ("SHAPE@", "OID@"), internal
                method used for cursors.

        Arg:
            fields: List or comma delimited field list.
        
        Returns:
            Concatenated string of the elements in the list of fields, 
                unless fields == '*'.
        """
        field_list = []
        if fields == '*':
            return fields
        elif isinstance(fields, six.string_types):
            fields = fields.split(',')
        if isinstance(fields, list):
            all_fields = self.list_fields()
            for f in fields:
                if '@' in f:
                    if f.upper() == SHAPE_TOKEN:
                        if self.ShapeFieldName:
                            field_list.append(self.ShapeFieldName)
                    elif f.upper() == OID_TOKEN:
                        if self.OIDFieldName:
                            field_list.append(self.OIDFieldName)
                else:
                    if f in all_fields:
                        field_list.append(f)
        return ','.join(field_list)

    def _format_server_response(self, server_response, records=None):
        """Returns a reformatted server response.
        
        Args:
            server_response: Server response to a request.
            records: Optional arg for records. Defaults to None.
        
        Returns:
            Either a GeoJSONFeatureSet or FeatureSet of the server response. 
                Can also return JSON of server response.
        """

        # set fields to full field definition of the layer
        if isinstance(server_response, requests.Response):
            server_response = munchify(server_response.json())
        flds = self.fieldLookup
        if FIELDS in server_response:
            for i,fld in enumerate(server_response.fields):
                server_response.fields[i] = flds.get(fld.name)

        if self.type == FEATURE_LAYER:
            for key in (FIELDS, GEOMETRY_TYPE, SPATIAL_REFERENCE):
                if key not in server_response:
                    if key == SPATIAL_REFERENCE:
                        server_response[key] = getattr(self, '_' + SPATIAL_REFERENCE)
                    else:
                        server_response[key] = getattr(self, key)

        # elif self.type == TABLE:
        if FIELDS not in server_response:
            server_response[FIELDS] = getattr(self, FIELDS)

        if all(map(lambda k: k in server_response, [FIELDS, FEATURES])):
            if records:
                server_response[FEATURES] = server_response[FEATURES][:records]
            return GeoJSONFeatureSet(server_response) if server_response.get(TYPE) == FEATURE_COLLECTION else FeatureSet(server_response)
        else:
            if records:
                if isinstance(server_response, list):
                    return server_response[:records]
            return server_response

    def _validate_params(self, where='1=1', fields='*', add_params={}, f=JSON, **kwargs):
        """Queries layer and gets response as JSON.

        Args:
            fields: Optional arg for fields to return. Default is "*" to 
                return all fields.
            where: Optional where clause. Defaults to '1=1'.
            add_params: Optional extra parameters to add to query string passed 
                as dict. Defaults to {}.
            f: Optional return format, default is JSON.  (html|json|kmz)
            kwargs: Optional extra parameters to add to query string passed as 
                key word arguments, will override add_params***.

        # default params for all queries
        params = {'returnGeometry' : 'true', 'outFields' : fields,
                  'where': where, 'f' : 'json'}
        """

        # default params
        params = {RETURN_GEOMETRY : TRUE, WHERE: where, F : f}

        for k,v in six.iteritems(add_params):
            params[k] = v

        for k,v in six.iteritems(kwargs):
            params[k] = v

        if RESULT_RECORD_COUNT in params and self.compatible_with_version('10.3'):
            params[RESULT_RECORD_COUNT] = min([int(params[RESULT_RECORD_COUNT]), self.get(MAX_RECORD_COUNT)])

        # check for tokens (only shape and oid)
        fields = self._fix_fields(fields)
        # print('FIX FIELDS OUTPUT: ', fields)
        params[OUT_FIELDS] = fields

        # geometry validation
        if self.type == FEATURE_LAYER and GEOMETRY in params:
            geom = Geometry(params.get(GEOMETRY))
            if SPATIAL_REL not in params:
                params[SPATIAL_REL] = ESRI_INTERSECT
            if isinstance(geom, Geometry):
                if IN_SR not in params and geom.getSR():
                    params[IN_SR] = geom.getSR()
                if getattr(geom, GEOMETRY_TYPE) and GEOMETRY_TYPE not in params:
                    params[GEOMETRY_TYPE] = getattr(geom, GEOMETRY_TYPE)

        elif self.type == TABLE:
            del params[RETURN_GEOMETRY]
        return params

    def iter_queries(self, where='1=1', add_params={}, max_recs=None, chunk_size=None, **kwargs):
        """Generator to form where clauses to query all records.  Will iterate 
                through "chunks" of OID's until all records have been returned 
                (grouped by maxRecordCount).

        *Thanks to Wayne Whitley for the brilliant idea to use izip_longest()



        Args:
            where: Optional where clause for OID selection.
            max_recs: Optional maximum amount of records returned for all 
                queries for OID fetch. Defaults to None.
            chunk_size: Optional size of chunks for each iteration of query 
                iterator. Defaults to None.
            add_params: Optional dictionary with any additional params you want 
                to add (can also use **kwargs). Defaults to {}.
        """

        if isinstance(add_params, dict):
            add_params[RETURN_IDS_ONLY] = TRUE

        # get oids
        resp = self.query(where=where, add_params=add_params)
        oids = sorted(resp.get(OBJECT_IDS, []))[:max_recs]
        oid_name = resp.get(OID_FIELD_NAME, OBJECTID)
        print('total records: {0}'.format(len(oids)))

        # set returnIdsOnly to False
        add_params[RETURN_IDS_ONLY] = FALSE

        # iterate through groups to form queries
        # overwrite max_recs here with transfer limit from service
        if chunk_size and chunk_size < self.json.get(MAX_RECORD_COUNT, 1000):
            max_recs = chunk_size
        else:
            max_recs = self.json.get(MAX_RECORD_COUNT, 1000)
        for each in zip_longest(*(iter(oids),) * max_recs):
            theRange = list(filter(lambda x: x != None, each)) # do not want to remove OID "0"
            if theRange:
                _min, _max = min(theRange), max(theRange)
                del each
                yield '{0} >= {1} and {0} <= {2}'.format(oid_name, _min, _max)


    def query(self, where='1=1', fields='*', add_params={}, records=None, exceed_limit=False, fetch_in_chunks=False, f=DEFAULT_REQUEST_FORMAT, kmz='', **kwargs):
        """Queries layer and gets response as JSON.
        
        Args:
            fields: Optional fields to return. Default is "*" to return all fields.
            where: Optional where clause. Defaults to '1:1'.
            add_params: Extra parameters to add to query string passed as dict.
            records: Number of records to return.  Default is None to return all
                records within bounds of max record count unless exceed_limit is True.
            exceed_limit: Option to get all records in layer.  This option may be time consuming
                because the ArcGIS REST API uses default maxRecordCount of 1000, so queries
                must be performed in chunks to get all records.  This is only 
                supported with JSON output. Default is False
            fetch_in_chunks: Option to return a generator with a FeatureSet in 
                chunks of each query group.  Use this to avoid memory errors when 
                fetching many features. Defaults to False
            f: Return format, default is JSON.  (html|json|kmz)
            kmz: Optional full path to output kmz file.  Only used if output 
                format is "kmz". Defaults to ''.
            kwargs: Optional extra parameters to add to query string passed as key word arguments,
                will override add_params***.

        # default params for all queries
        params: {'returnGeometry' : 'true', 'outFields' : fields,
        'where': where, 'f' : 'json'}
        
        Returns:
            Response as JSON.
        """
        query_url = self.url + '/query'

        params = self._validate_params(where, fields, add_params, f, **kwargs)

        # create kmz file if requested (does not support exceed_limit parameter)
        if f == 'kmz':
            r = self.request(query_url, params)
            r.raw.decode_content = True
            r.encoding = 'zlib_codec'

            # write kmz using codecs
            if not kmz:
                kmz = validate_name(os.path.join(os.path.expanduser('~'), 'Desktop', '{}.kmz'.format(self.name)))
            with codecs.open(kmz, 'wb') as f:
                f.write(r.content)
            print('creating kmz')
##            with open(kmz, 'wb') as f:
##                shutil.copyfileobj(r.raw, f)
            print('Created: "{0}"'.format(kmz))
            return kmz

        else:
            server_response = {}
            if exceed_limit:

                for i, where2 in enumerate(self.iter_queries(where, params, max_recs=records)):
                    sql = ' and '.join(filter(None, [where.replace('1=1', ''), where2])) #remove default
                    params[WHERE] = sql
                    resp = self.request(query_url, params)
                    if i < 1:
                        server_response = resp
                    else:
                        server_response[FEATURES] += resp[FEATURES]

            else:
                if isinstance(records, int) and str(self.currentVersion) >= '10.3':
                    params[RESULT_RECORD_COUNT] = records

                server_response = self.request(query_url, params)

            return self._format_server_response(server_response, records)

    def query_in_chunks(self, where='1=1', fields='*', add_params={}, records=None, **kwargs):
        """Queries a layer in chunks and returns a generator.
        
        Args:
            fields: Optional fields to return. Default is "*" to return all fields.
            where: Optional where clause. Defaults to '1=1'.
            add_params: Optional extra parameters to add to query string passed as dict.
            records: Optional number of records to return.  Default is None to 
                return all. Records within bounds of max record count unless 
                exceed_limit is True.
            kwargs: Optional extra parameters to add to query string passed as key word arguments,
                will override add_params***.

        # default params for all queries
        params: {'returnGeometry' : 'true', 'outFields' : fields,
        'where': where, 'f' : 'json'}
        """

        query_url = self.url + '/query'

        params = self._validate_params(where, fields, add_params, **kwargs)
        for where2 in self.iter_queries(where, params, max_recs=records):
            sql = ' and '.join(filter(None, [where.replace('1=1', ''), where2])) #remove default
            params[WHERE] = sql
            # print('FIELDS: ', params.get('fields'))
            yield self._format_server_response(self.request(query_url, params))


    def query_related_records(self, objectIds, relationshipId, outFields='*', definitionExpression=None, returnGeometry=None, outSR=None, **kwargs):
        """Queries related records.
        
        Args:
            objectIds: List of object ids for related records.
            relationshipId: ID of relationship.
            outFields: Optional output fields for related features. Defaults to '*'.
            definitionExpression: Optional def query for output related records. 
                Defaults to None.
            returnGeometry: Option to return Geometry. Defaults to None.
            outSR: Optional output spatial reference, defaults to None.
            kwargs: Optional key word args for REST API.
        
        Raises:
            NotImplementedError: 'This Resource does not have any relationships!'
        
        Returns:
            The related records.
        """
        
        if not self.json.get(RELATIONSHIPS):
            raise NotImplementedError('This Resource does not have any relationships!')

        if isinstance(objectIds, (list, tuple)):
            objectIds = ','.join(map(str, objectIds))

        query_url = self.url + '/queryRelatedRecords'
        params = {
            OBJECT_IDS: objectIds,
            RELATIONSHIP_ID: relationshipId,
            OUT_FIELDS: outFields,
            DEFINITION_EXPRESSION: definitionExpression,
            RETURN_GEOMETRY: returnGeometry,
            OUT_SR: outSR
        }

        for k,v in six.iteritems(kwargs):
            params[k] = v
        return RelatedRecords(self.request(query_url, params))

    def select_by_location(self, geometry, geometryType='', inSR='', spatialRel=ESRI_INTERSECT, distance=0, units=ESRI_METER, add_params={}, **kwargs):
        """Selects features by location of a geometry, returns a feature set.
    
        Args:
            geometry: Geometry as JSON.
            geometryType: Optional type of geometry object, this can be gleaned
                 automatically from the geometry input. Defaults to ''.
            inSR: Optional input spatial reference. Defaults to ''.
            spatialRel: Optional spatial relationship applied on the input geometry 
                when performing the query operation. Defaults to ESRI_INTERSECT.
            distance: Optional distance for search. Defaults to 0.
            units: Optional units for distance, only used if distance > 0.
            add_params: Optional dict containing any other options that will be 
                added to the query. Defaults to {}.
            kwargs: Optional keyword args to add to the query.
        
        Spatial Relationships:
            esriSpatialRelIntersects | esriSpatialRelContains | esriSpatialRelCrosses | esriSpatialRelEnvelopeIntersects | esriSpatialRelIndexIntersects
            | esriSpatialRelOverlaps | esriSpatialRelTouches | esriSpatialRelWithin | esriSpatialRelRelation
        
        Unit Options:
            esriSRUnit_Meter | esriSRUnit_StatuteMile | esriSRUnit_Foot | esriSRUnit_Kilometer | esriSRUnit_NauticalMile | esriSRUnit_USNauticalMile
        """
        geometry = Geometry(geometry)
        if not geometryType:
            geometryType = geometry.geometryType
        if not inSR:
            inSR = geometry.getSR()

        params = {GEOMETRY: geometry,
                  GEOMETRY_TYPE: geometryType,
                  SPATIAL_REL: spatialRel,
                  IN_SR: inSR,
            }

        if int(distance):
            params[DISTANCE] = distance
            params[UNITS] = units

        # add additional params
        for k,v in six.iteritems(add_params):
            if k not in params:
                params[k] = v

        # add kwargs
        for k,v in six.iteritems(kwargs):
            if k not in params:
                params[k] = v

        return FeatureSet(self.query(add_params=params))

    def layer_to_kmz(self, out_kmz='', flds='*', where='1=1', params={}):
        """Method to create kmz from query.
        
        Args:
            out_kmz: Optional output kmz file path, if none specified will be saved on Desktop. Defaults to ''.
            flds: Optional list of fields for fc. If none specified, all fields 
                are returned. Defaults to '*'. Supports fields in list [] or comma 
                separated string "field1,field2,.."
            where: Optional where clause, defaults to '1=1'.
            params: Optional dictionary of parameters for query, Defaults to {}.
        """
        return query(self.url, flds, where=where, add_params=params, ret_form='kmz', token=self.token, kmz=out_kmz)

    def getOIDs(self, where='1=1', max_recs=None, **kwargs):
        """Returns a list of OIDs from feature layer.
        
        Args:
            where: Optional where clause for OID selection. Defaults to '1=1'.
            max_recs: Optional maximimum number of records to return 
                (maxRecordCount does not apply). Defaults to None.
            **kwargs: Optional key word arguments to further limit query (i.e. add geometry interesect).
        """

        p = {RETURN_IDS_ONLY:TRUE,
             RETURN_GEOMETRY: FALSE,
             OUT_FIELDS: ''}

        # add kwargs if specified
        for k,v in six.iteritems(kwargs):
            if k not in p.keys():
                p[k] = v

        return sorted(self.query(where=where, add_params=p)[OBJECT_IDS])[:max_recs]

    def getCount(self, where='1=1', **kwargs):
        """Returns count of features, can use optional query and **kwargs to filter.
        
        Args:
            where: Optional where clause, defaults to '*'.
            kwargs: Optional keyword arguments for query operation. 
        """

        return len(self.getOIDs(where,  **kwargs))

    def attachments(self, oid, gdbVersion=''):
        """Queries attachments for an OBJECTDID.
        
        Args:
            oid: Object ID.
            gdbVersion: Optional Geodatabase version to query, defaults to ''.
        
        Raises:
            NotImplementedError: 'Layer "{}" does not support attachments!'
        
        Returns:
            Attachments for OID.
        """

        if self.hasAttachments:
            query_url = '{0}/{1}/attachments'.format(self.url, oid)
            r = self.request(query_url, { F: JSON }, ret_json=True)

            add_tok = ''
            if self.token:
                add_tok = '?token={}'.format(self.token.token if isinstance(self.token, Token) else self.token)

            if ATTACHMENT_INFOS in r:
                for attInfo in r[ATTACHMENT_INFOS]:
                    att_url = '{}/{}'.format(query_url, attInfo[ID])
                    attInfo[URL] = att_url
                    if self._proxy:
                        attInfo[URL_WITH_TOKEN] = '?'.join([self._proxy, att_url])
                    else:
                        attInfo[URL_WITH_TOKEN] = att_url + ('?token={}'.format(self.token) if self.token else '')

                keys = []
                if r[ATTACHMENT_INFOS]:
                    keys = r[ATTACHMENT_INFOS][0].keys()

                props = list(set(['id', 'name', 'size', 'contentType', 'url', 'urlWithToken'] + list(keys)))

                class Attachment(namedtuple('Attachment', ' '.join(props))):
                    """Class to handle Attachment object."""
                    __slots__ = ()
                    def __new__(cls,  **kwargs):
                        return super(Attachment, cls).__new__(cls, **kwargs)

                    def __repr__(self):
                        if hasattr(self, ID) and hasattr(self, NAME):
                            return '<Attachment ID: {} ({})>'.format(self.id, self.name)
                        else:
                            return '<Attachment> ?'

                    def blob(self):
                        """Returns a string of the chunks in the response."""
                        b = ''
                        resp = requests.get(getattr(self, URL_WITH_TOKEN), stream=True, verify=False)
                        for chunk in resp.iter_content(1024 * 16):
                            b += chunk
                        return b

                    def download(self, out_path, name='', verbose=True):
                        """Downloads the attachment to specified path.
                        
                        Args:
                            out_path: Output path for attachment.
                            name: Optional name for output file.  If left blank, 
                                will be same as attachment.
                            verbose: Optional boolean, if true will print sucessful 
                                download message. Defaults to True.
                        
                        Returns:
                            The path to the downloaded attachment.
                        """

                        if not name:
                            out_file = assign_unique_name(os.path.join(out_path, self.name))
                        else:
                            ext = os.path.splitext(self.name)[-1]
                            out_file = os.path.join(out_path, name.split('.')[0] + ext)

                        resp = requests.get(getattr(self, URL_WITH_TOKEN), stream=True, verify=False)
                        with open(out_file, 'wb') as f:
                            for chunk in resp.iter_content(1024 * 16):
                                f.write(chunk)

                        if verbose:
                            print('downloaded attachment "{}" to "{}"'.format(self.name, out_path))
                        return out_file

                return [Attachment(**a) for a in r[ATTACHMENT_INFOS]]

            return []

        else:
            raise NotImplementedError('Layer "{}" does not support attachments!'.format(self.name))

    def cursor(self, fields='*', where='1=1', add_params={}, records=None, exceed_limit=False):
        """Runs Cursor on layer, helper method that calls Cursor Object.
        
        Args:
            fields: Optional fields to return. Default is "*" to return all fields.
            where: Optional where clause. Defaults to '1=1'.
            add_params: Optional extra parameters to add to query string passed
                as dict. Defaults to {}.
            records: Optional number of records to return.  Default is None to 
                return all. records within bounds of max record count unless 
                exceed_limit is True.
            exceed_limit: Optional boolean to get all records in layer.  This 
                option may be time consuming because the ArcGIS REST API uses 
                default maxRecordCount of 1000, so queries must be performed in 
                chunks to get all records.
        """

        cur_fields = self._fix_fields(fields)

        fs = self.query(where, cur_fields, add_params, records, exceed_limit)
        return Cursor(fs, fields)

    def export_layer(self, out_fc, fields='*', where='1=1', records=None, params={}, exceed_limit=False, sr=None,
                     include_domains=True, include_attachments=False, qualified_fieldnames=False, **kwargs):
        """Method to export a feature class or shapefile from a service layer.
        
        Args:
            out_fc: Full path to output feature class.
            where: Optional where clause. Defaults to '1=1'.
            params: Optional dictionary of parameters for query. Defaults to {}.
            fields: Optional list of fields for fc. If none specified, all fields 
                are returned. Defaults to '*'. Supports fields in list [] or comma 
                separated string "field1,field2,..".
            records: Optional number of records to return. Default is None, will 
                return maxRecordCount.
            exceed_limit: Optional boolean to get all records.  If True, will 
                recursively query REST endpoint until all records have been 
                gathered. Default is False.
            sr: Optional output spatial refrence (WKID). Defaults to None.
            include_domains: Optional boolean, if True, will manually create 
                the feature class and add domains to GDB if output is in a 
                geodatabase. Default is False.
            include_attachments: Optional boolean, if True, will export features 
                with attachments.  This argument is ignored when the "out_fc" param 
                is not a feature class, or the ObjectID field is not included in 
                "fields" param or if there is no access to arcpy. Defaults to False.
            qualified_fieldnames: Optional boolean to keep qualified field names, 
                default is False.
            
        Returns:
            A feature class or shapefile.
        """
        
        if self.type in (FEATURE_LAYER, TABLE):

            # make new feature class
            if not sr:
                sr = self.getSR()
            else:
                params[OUT_SR] = sr

            if exceed_limit:

                # download in chunks
                isShp = out_fc.endswith('.shp')
                orig = out_fc
                doesExceed = False
                if isShp:
                    if self.query(returnCountOnly=True).count > self.maxRecordCount:
                        doesExceed = True
                        out_fc = r'in_memory\restapi_chunk_{}'.format(os.path.splitext(os.path.basename(orig))[0])
                for fs in self.query_in_chunks(where, fields, params, **kwargs):
                    exportFeatureSet(fs, out_fc, include_domains=False)

                if not isShp and include_domains:
                    add_domains_from_feature_set(out_fc, fs)

                if has_arcpy and isShp and doesExceed:
                    arcpy.management.CopyFeatures(out_fc, orig)
                    arcpy.management.Delete(out_fc)
                    out_fc = orig

                print('Fetched all records')
                return out_fc

            else:

                # do query to get feature set
                fs = self.query(where, fields, params, records, exceed_limit, **kwargs)

                # get any domain info
                f_dict = {f.name: f for f in self.fields}
                for field in fs.fields:
                    if field:
                        field.domain = f_dict[field.name].get(DOMAIN)

                return exportFeatureSet(fs, out_fc, include_domains)

##            if has_arcpy and all([include_attachments, self.hasAttachments, fs.OIDFieldName])::
##                export_attachments()
##

        else:
            print('Layer: "{}" is not a Feature Layer!'.format(self.name))


    def clip(self, poly, output, fields='*', out_sr='', where='', envelope=False, exceed_limit=True, **kwargs):
        """Method for spatial Query, exports geometry that intersect polygon or
                envelope features.
        
        Args:
            poly: Polygon (or other) features used for spatial query.
            output: Full path to output feature class.
            fields: Optional list of fields for fc. If none specified, all fields are returned.
                Supports fields in list [] or comma separated string 
                "field1,field2,..". Defaults to '*'.
            out_sr: Optional output spatial refrence (WKID). Defaults to ''.
            where: Optional where clause. Defaults to ''.
            envelope: Optional boolean, if True, the polygon features bounding 
                box will be used. This option can be used if the feature has 
                many vertices or to check against the full extent of the feature 
                class. Defaults to False.
        """

        in_geom = Geometry(poly)
        sr = in_geom.getSR()
        if envelope:
            geojson = in_geom.envelopeAsJSON()
            geometryType = ESRI_ENVELOPE
        else:
            geojson = in_geom.dumps()
            geometryType = in_geom.geometryType

        if not out_sr:
            out_sr = sr

        d = {GEOMETRY_TYPE: geometryType,
             RETURN_GEOMETRY: TRUE,
             GEOMETRY: geojson,
             IN_SR : sr,
             OUT_SR: out_sr,
             SPATIAL_REL: kwargs.get(SPATIAL_REL) or ESRI_INTERSECT
        }
        return self.export_layer(output, fields, where, params=d, exceed_limit=True, sr=out_sr)

    def __repr__(self):
        """String representation with service name."""
        return '<{}: "{}" (id: {})>'.format(self.__class__.__name__, self.name, self.id)

class MapServiceTable(MapServiceLayer):
    pass

    def export_table(self, *args, **kwargs):
        """Method to export a feature class or shapefile from a service layer.
        
        Args:
            out_fc: Full path to output feature class.
            where: Optional where clause.
            params: Optional dictionary of parameters for query.
            fields: List of fields for fc. If none specified, all fields are 
                returned. Supports fields in list [] or comma separated string 
                "field1,field2,..".
            records: Optional number of records to return. Default is none, will 
                return maxRecordCount
            exceed_limit: Optional boolean to get all records.  If true, will 
                recursively query REST endpoint until all records have been 
                gathered. Default is False.
            sr: Optional output spatial refrence (WKID)
            include_domains: Optional boolean, if True, will manually create the 
                feature class and add domains to GDB if output is in a geodatabase.
            include_attachments: Optional boolean, if True, will export features 
                with attachments.  This argument is ignored when the "out_fc" param 
                is not a feature class, or the ObjectID field is not included in 
                "fields" param or if there is no access to arcpy.
        """
        
        return self.export_layer(*args, **kwargs)

    def clip(self):
        raise NotImplemented('Tabular Data cannot be clipped!')

    def select_by_location(self):
        raise NotImplemented('Select By Location not supported for tabular data!')

    def layer_to_kmz(self):
        raise NotImplemented('Tabular Data cannot be converted to KMZ!')

# LEGACY SUPPORT
MapServiceLayer.layer_to_fc = MapServiceLayer.export_layer

class MapService(BaseService):
    """Class that handles map services."""
    def getLayerIdByName(self, name, grp_lyr=False):
        """Gets a mapservice layer ID by layer name from a service (returns an integer).
        
        Args:
            name: Name of layer from which to grab ID.
            grp_lyr: Optional boolean, default is False, does not return layer ID 
                for group layers. Set to true to search for group layers too.
        """

        all_layers = self.layers
        for layer in all_layers:
            if fnmatch.fnmatch(fix_encoding(layer.get(NAME)), fix_encoding(name)):
                if SUB_LAYER_IDS in layer:
                    if grp_lyr and layer[SUB_LAYER_IDS] != None:
                        return layer[ID]
                    elif not grp_lyr and not layer[SUB_LAYER_IDS]:
                        return layer[ID]
                return layer[ID]
        for tab in self.tables:
            if fnmatch.fnmatch(fix_encoding(tab.get(NAME)), fix_encoding(name)):
                return tab.get(ID)
        print('No Layer found matching "{0}"'.format(name))
        return None

    def get_layer_url(self, name, grp_lyr=False):
        """Returns the fully qualified path to a layer url by pattern match on name,
                will return the first match.
        Args:
            name: Name of layer from which to grab ID.
            grp_lyr: Optional boolean, default is false, does not return layer 
                ID for group layers. Set to true to search for group layers too.
        """

        return '/'.join([self.url, str(self.getLayerIdByName(name,grp_lyr))])

    def list_layers(self):
        """Method to return a list of layer names in a MapService."""
        return [fix_encoding(l.name) for l in self.layers]

    def list_tables(self):
        """Method to return a list of layer names in a MapService."""
        return [t.name for t in self.tables]

    def getNameFromId(self, lyrID):
        """Method to get layer name from ID.

        Arg:
            lyrID: ID of layer for which to get name.
        
        Returns:
            Layer name.
        """

        return [fix_encoding(l.name) for l in self.layers if l.id == lyrID][0]

    def export(self, out_image=None, imageSR=None, bbox=None, bboxSR=None, size=None, dpi=96, format='png', transparent=True, urlOnly=False, **kwargs):
        """Exports a map image.
        
        Args:
            out_image: Full path to output image.
            imageSR: Optional spatial reference for exported image. Defaults to None.
            bbox: Optional bounding box as comma separated string. Defaults to None.
            bboxSR: Optional spatial reference for bounding box. Defaults to None.
            size: Optional comma separated string for the size of image in pixels. 
                It is advised not to use this arg and let this method generate 
                it automatically. Defaults to None.
            dpi: Optional output resolution, default is 96.
            format: Optional image format, default is png8.
            transparent: Optional boolean to support transparency in exported 
                image, default is True.
            kwargs: Any additional keyword arguments for export operation 
                (must be supported by REST API).
            
        Keyword Arguments can be found here:
            http://resources.arcgis.com/en/help/arcgisrest:api/index.html#/Export_Map/02r3000000v7000000/
        """

        query_url = self.url + '/export'
        # defaults if params not specified
        if bbox and not size:
            if isinstance(bbox, (list, tuple)):
                size = ','.join(map(str, [abs(int(bbox[0]) - int(bbox[2])), abs(int(bbox[1]) - int(bbox[3]))]))

        if isinstance(bbox, dict) or (isinstance(bbox, six.string_types) and bbox.startswith('{')):
            print('it is a geometry object')
            bbox = Geometry(bbox)

        if isinstance(bbox, Geometry):
            geom = bbox
            bbox = geom.envelope()
            bboxSR = geom.spatialReference
            envJson = geom.envelopeAsJSON()
            size = ','.join(map(str, [abs(envJson.get(XMAX) - envJson.get(XMIN)), abs(envJson.get(YMAX) - envJson.get(YMIN))]))
            print('set size from geometry object: {}'.format(size))

        if not bbox:
            ie = self.initialExtent
            bbox = ','.join(map(str, [ie.xmin, ie.ymin, ie.xmax, ie.ymax]))

            if not size:
                size = ','.join(map(str, [abs(int(ie.xmin) - int(ie.xmax)), abs(int(ie.ymin) - int(ie.ymax))]))

            bboxSR = self.spatialReference

        if not imageSR:
            imageSR = self.spatialReference

        # initial params
        params = {FORMAT: format,
          F: IMAGE,
          IMAGE_SR: imageSR,
          BBOX_SR: bboxSR,
          BBOX: bbox,
          TRANSPARENT: transparent,
          DPI: dpi,
          SIZE: size
        }

        # add additional params from **kwargs
        for k,v in six.iteritems(kwargs):
            if k not in params:
                params[k] = v

        if urlOnly:
            if self.token:
                params[TOKEN] = self.token
            return query_url + '?' + six.moves.urllib.parse.urlencode(params)

        # do post
        r = self.request(query_url, params, ret_json=False)

        # save image
        with open(out_image, 'wb') as f:
            f.write(r.content)

        return r

    def layer(self, name_or_id, **kwargs):
        """Method to return a layer object with advanced properties by name.

        Arg:
            name_or_id: Layer name (supports wildcard syntax*) or id 
                (must be of type <int>).
        """

        if isinstance(name_or_id, int):
            # reference by id directly
            return MapServiceLayer('/'.join([self.url, str(name_or_id)]), token=self.token)

        layer_path = self.get_layer_url(name_or_id, self.token, **kwargs)
        if layer_path:
            return MapServiceLayer(layer_path, token=self.token, **kwargs)
        else:
            print('Layer "{0}" not found!'.format(name_or_id))

    def table(self, name_or_id):
        """Method to return a layer object with advanced properties by name.

        Arg:
            name_or_id: Table name (supports wildcard syntax*) or id (must be of type <int>).
        """
        if isinstance(name_or_id, int):
            # reference by id directly
            return MapServiceTable('/'.join([self.url, str(name_or_id)]), token=self.token)

        layer_path = self.get_layer_url(name_or_id, self.token)
        if layer_path:
            return MapServiceTable(layer_path, token=self.token)
        else:
            print('Table "{0}" not found!'.format(name_or_id))

    def cursor(self, layer_name, fields='*', where='1=1', records=None, add_params={}, exceed_limit=False):
        """Cursor object to handle queries to rest endpoints.
        
        Args:
            layer_name: Name of layer in map service.
            fields: Option to limit fields returned.  All are returned by 
                default, '*'.
            where: Optional where clause for cursor, '1=1'.
            records: Optional number of records to return 
                (within bounds of max record count). Defaults to None.
            add_params: Optional boolean to add additional search parameters. 
                Defaults to {}.
            exceed_limit: Optional boolean to get all records in layer. 
                Defaults to False. This option may be time consuming because the 
                ArcGIS REST API uses default maxRecordCount of 1000, so queries
                must be performed in chunks to get all records.
        """

        lyr = self.layer(layer_name)
        return lyr.cursor(fields, where, add_params, records, exceed_limit)

    def export_layer(self, layer_name,  out_fc, fields='*', where='1=1',
                    records=None, params={}, exceed_limit=False, sr=None):
        """Method to export a feature class from a service layer.
        
        Args:
            layer_name: Name of map service layer to export to fc.
            out_fc: Full path to output feature class.
            where: Optional where clause. Defaults to '1=1'.
            params: Optional dictionary of parameters for query. Defaults to {}.
            fields: Optional list of fields for fc. If none specified, all fields 
                are returned. Supports fields in list [] or comma separated string 
                "field1,field2,..". Defaults to '*'.
            records: Optional number of records to return. Default is none, 
                will return maxRecordCount.
            exceed_limit: Optional boolean to get all records.  If true, will 
                recursively query REST endpoint until all records have been gathered. 
                Default is False.
            sr: Optional output spatial refrence (WKID). Defaults to None.
        """
        
        lyr = self.layer(layer_name)
        lyr.layer_to_fc(out_fc, fields, where,records, params, exceed_limit, sr)

    def layer_to_kmz(self, layer_name, out_kmz='', flds='*', where='1=1', params={}):
        """Method to create kmz from query.
        
        Args:
            layer_name: Name of map service layer to export to fc.
            out_kmz: Optional output kmz file path, if none specified will be saved
                on Desktop. Defaults to ''.
            flds: Optional list of fields for fc. If none specified, all fields are returned.
                Supports fields in list [] or comma separated string 
                "field1,field2,..". Defaults to '*'.
            where: Optional where clause. Defaults to '1=1'.
            params: Optional dictionary of parameters for query, defaults to {}.
        """

        lyr = self.layer(layer_name)
        lyr.layer_to_kmz(flds, where, params, kmz=out_kmz)

    def clip(self, layer_name, poly, output, fields='*', out_sr='', where='', envelope=False):
        """Method for spatial Query, exports geometry that intersect polygon or
                envelope features.
        
        Args:
            layer_name: Name of map service layer to export to fc.
            poly: Polygon (or other) features used for spatial query.
            output: Full path to output feature class.
            fields: Optional list of fields for fc. If none specified, all fields are 
                returned. Supports fields in list [] or comma separated string 
                "field1,field2,.."
            sr: Optional output spatial refrence (WKID)
            where: Optional where clause. Defaults to ''.
            envelope: Optional boolean, if true, the polygon features bounding 
                box will be used.  This option can be used if the feature has 
                many vertices or to check against the full extent of the feature 
                class.
        
        Returns:
            A clip of the layer.
        """

        lyr = self.layer(layer_name)
        return lyr.clip(poly, output, fields, out_sr, where, envelope)

    def __iter__(self):
        for lyr in self.layers:
            yield lyr

# Legacy support
MapService.layer_to_fc = MapService.export_layer

class FeatureService(MapService):
    """Class to handle Feature Service.

    Args:
        url: Image service url.
    (below args only required if security is enabled):
        usr: Username credentials for ArcGIS Server.
        pw: Password credentials for ArcGIS Server.
        token: Token to handle security (alternative to usr and pw).
        proxy: Option to use proxy page to handle security, need to provide
            full path to proxy url.
    """

    @property
    def replicas(self):
        """Returns a list of replica objects."""
        if self.syncEnabled:
            reps = self.request(self.url + '/replicas')
            return [namedTuple('Replica', r) for r in reps]
        else:
            return []

    def query(self, **kwargs):
        if LAYER_DEFS not in kwargs:
            kwargs[LAYER_DEFS] = json.dumps([{ 'layerId': l.id } for l in self.layers])
        resp = self.request(self.url + '/query', **kwargs)
        return list(map(lambda fs: FeatureSet(fs), filter(lambda x: len(x.get(FEATURES, [])), resp.get(LAYERS, {}))))
                

    def layer(self, name_or_id):
        """Method to return a layer object with advanced properties by name.

        Arg:
            name: Layer name (supports wildcard syntax*) or layer id (int).
        """

        if isinstance(name_or_id, int):
            # reference by id directly
            return FeatureLayer('/'.join([self.url, str(name_or_id)]), token=self.token)

        layer_path = self.get_layer_url(name_or_id)
        if layer_path:
            return FeatureLayer(layer_path, token=self.token)
        else:
            print('Layer "{0}" not found!'.format(name_or_id))

    def layer_to_kmz(self, layer_name, out_kmz='', flds='*', where='1=1', params={}):
        """Method to create kmz from query.
        
        Args:
            layer_name: Name of map service layer to export to fc.
            out_kmz: Optional output kmz file path, if none specified will be saved on Desktop
            flds: Optional list of fields for fc. If none specified, all fields 
                are returned. Supports fields in list [] or comma separated 
                string "field1,field2,..". Default is '*'.
            where: Optional where clause.
            params: Optional dictionary of parameters for query.
        """

        lyr = self.layer(layer_name)
        lyr.layer_to_kmz(flds, where, params, kmz=out_kmz)

    def createReplica(self, layers, replicaName, geometry='', geometryType='', inSR='', replicaSR='', dataFormat='json', returnReplicaObject=True, **kwargs):
        """Queries attachments, returns a JSON object.
        
        Args:
            layers: List of layers to create replicas for (valid inputs below).
            replicaName: Name of replica.
            geometry: Optional geometry to query features, if none supplied, 
                will grab all features. Defaults to ''.
            geometryType: Optional type of geometry. Defaults to ''.
            inSR: Optional innput spatial reference for geometry. Defaults to ''.
            replicaSR: Optional output spatial reference for replica data.
                Defaults to ''.
            dataFormat: Optional output format for replica (sqlite|json).
                Defaults to 'json'.
            **kwargs: Optional keyword arguments for createReplica request.
            returnReplicaObject : Special optional arg to return replica as an 
                object (restapi.SQLiteReplica|restapi.JsonReplica) based on the 
                dataFormat of the replica.  If the data format is sqlite and this 
                parameter is False, the data will need to be fetched quickly because 
                the server will automatically clean out the directory. The default cleanup 
                for a sqlite file is 10 minutes. This option is set to True
                by default.  It is recommended to set this option to True if 
                the output dataFormat is "sqlite".
        
        Documentation on Server Directory Cleaning:
        http://server.arcgis.com/en/server/latest/administer/linux/aboutserver:directories.htm
        """

        if hasattr(self, SYNC_ENABLED) and not self.syncEnabled:
            raise NotImplementedError('FeatureService "{}" does not support Sync!'.format(self.url))

        # validate layers
        if isinstance(layers, six.string_types):
            layers = [l.strip() for l in layers.split(',')]

        elif not isinstance(layers, (list, tuple)):
            layers = [layers]

        if all(map(lambda x: isinstance(x, int), layers)):
            layers = ','.join(map(str, layers))

        elif all(map(lambda x: isinstance(x, six.string_types), layers)):
            layers = ','.join(map(str, filter(lambda x: x is not None,
                                [s.id for s in self.layers if s.name.lower()
                                 in [l.lower() for l in layers]])))

        if not geometry and not geometryType:
            ext = self.initialExtent
            inSR = self.initialExtent.spatialReference
            geometry= ','.join(map(str, [ext.xmin,ext.ymin,ext.xmax,ext.ymax]))
            geometryType = ESRI_ENVELOPE
            inSR = self.spatialReference
            useGeometry = False
        else:
            useGeometry = True
            geometry = Geometry(geometry)
            inSR = geometry.getSR()
            geometryType = geometry.geometryType


        if not replicaSR:
            replicaSR = self.spatialReference

        validated = layers.split(',')
        options = {REPLICA_NAME: replicaName,
                   LAYERS: layers,
                   LAYER_QUERIES: '',
                   GEOMETRY: geometry,
                   GEOMETRY_TYPE: geometryType,
                   IN_SR: inSR,
                   REPLICA_SR:	replicaSR,
                   TRANSPORT_TYPE: TRANSPORT_TYPE_URL,
                   RETURN_ATTACHMENTS:	TRUE,
                   RETURN_ATTACHMENTS_DATA_BY_URL: TRUE,
                   ASYNC:	FALSE,
                   F: PJSON,
                   DATA_FORMAT: dataFormat,
                   REPLICA_OPTIONS: '',
                   }

        for k,v in six.iteritems(kwargs):
            if k != SYNC_MODEL:
                if k == LAYER_QUERIES:
                    if options[k]:
                        if isinstance(options[k], six.string_types):
                            options[k] = json.loads(options[k])
                        for key in options[k].keys():
                            options[k][key][USE_GEOMETRY] = useGeometry
                            options[k] = json.dumps(options[k], ensure_ascii=False)
                else:
                    options[k] = v

        if self.syncCapabilities.supportsPerReplicaSync:
            options[SYNC_MODEL] = PER_REPLICA
        else:
            options[SYNC_MODEL] = PER_LAYER

        if options[ASYNC] in (TRUE, True) and self.syncCapabilities.supportsAsync:
            st = self.request(self.url + '/createReplica', options, )
            while STATUS_URL not in st:
                time.sleep(1)
        else:
            options[ASYNC] = 'false'
            st = self.request(self.url + '/createReplica', options)

        if returnReplicaObject:
            return self.fetchReplica(st)
        else:
            return st

    @staticmethod
    def fetchReplica(rep_url):
        """Fetches and returns a replica from a server resource.  This can be a 
                url or a dictionary/JSON object with a "URL" key.  Based on the 
                file name of the replica, this will return either a 
                restapi.SQLiteReplica() or restapi.JsonReplica() object.  The 
                two valid file name extensions are ".json" (restapi.JsonReplica) 
                or ".geodatabase" (restapi.SQLiteReplica).
        
        Arg:
            rep_url : url or JSON object that contains url to replica file on server.
            
        If the file is sqlite, it is highly recommended to use a with statement to
        work with the restapi.SQLiteReplica object so the connection is automatically
        closed and the file is cleaned from disk.  Example:

            >>> url = 'http://someserver.com/arcgis/rest/directories/TEST/SomeService_MapServer/_ags_data{B7893BA273C164D96B7BEE588627B3EBC}.geodatabase'
            >>> with FeatureService.fetchReplica(url) as replica:
            >>>     # this is a restapi.SQLiteReplica() object
            >>>     # list tables in database
            >>>     print(replica.list_tables())
            >>>     # export to file geodatabase < requires arcpy access
            >>>     replica.exportToGDB(r'C\Temp\replica.gdb')
        """

        if isinstance(rep_url, dict):
            rep_url = st.get(URL_UPPER)

        if rep_url.endswith('.geodatabase'):
            resp = requests.get(rep_url, stream=True, verify=False)
            fileName = rep_url.split('/')[-1]
            db = os.path.join(TEMP_DIR, fileName)
            with open(db, 'wb') as f:
                for chunk in resp.iter_content(1024 * 16):
                    if chunk:
                        f.write(chunk)
            return SQLiteReplica(db)

        elif rep_url.endswith('.json'):
            return JsonReplica(requests.get(self.url, verify=False).json())

        return None


    def replicaInfo(self, replicaID):
        """Gets replica information.

        Arg:
            replicaID: ID of replica.
        
        Returns:
            A named tuple.
        """

        query_url = self.url + '/replicas/{}'.format(replicaID)
        return namedTuple('ReplicaInfo', self.request(query_url))

    def syncReplica(self, replicaID, **kwargs):
        """Synchronize a replica.  Must be called to sync edits before a fresh 
                replica can be obtained next time createReplica is called.  
                Replicas are snapshots in time of the first time the user creates 
                a replica, and will not be reloaded until synchronization has 
                occured. A new version is created for each subsequent replica, 
                but it is cached data.

        It is also recommended to unregister a replica
        AFTER sync has occured.  Alternatively, setting the "closeReplica" keyword
        argument to True will unregister the replica after sync.

        More info can be found here:
            http://server.arcgis.com/en/server/latest/publish-services/windows/prepare-data-for-offline-use.htm

        and here for key word argument parameters:
            http://resources.arcgis.com/en/help/arcgis-rest-api/index.html#/Synchronize_Replica/02r3000000vv000000/

        Arg:
            replicaID: ID of replica.
        """

        query_url = self.url + '/synchronizeReplica'
        params = {REPLICA_ID: replicaID}

        for k,v in six.iteritems(kwargs):
            params[k] = v

        return self.request(query_url, params)


    def unRegisterReplica(self, replicaID):
        """Unregisters a replica on the feature service.

        Arg:
            replicaID: The ID of the replica registered with the service.
        """

        query_url = self.url + '/unRegisterReplica'
        params = {REPLICA_ID: replicaID}
        return self.request(query_url, params)

class FeatureLayer(MapServiceLayer):
    """Class to handle Feature Service Layer."""
    def __init__(self, url='', usr='', pw='', token='', proxy=None, referer=None):
        """Inits class with url and login credentials.
        
        Args:
            url: Feature service layer url.
        (below args only required if security is enabled):
            usr: Username credentials for ArcGIS Server. Defaults to ''.
            pw: Password credentials for ArcGIS Server. Defaults to ''.
            token: Token to handle security (alternative to usr and pw) Default is ''.
            proxy: Option to use proxy page to handle security, need to provide
                full path to proxy url. Defaults to None.
            referer: Option to add Referer Header if required by proxy, this parameter
                is ignored if no proxy is specified. Defaults to None.
        """

        super(FeatureLayer, self).__init__(url, usr, pw, token, proxy, referer)

        # store list of EditResult() objects to track changes
        self.editResults = []

    def updateCursor(self, fields='*', where='1=1', add_params={}, records=None, exceed_limit=False, auto_save=True, useGlobalIds=False, **kwargs):
        """Updates features in layer using a cursor, the applyEdits() method is 
                automatically called when used in a "with" statement and auto_save is True.
        
        Args:
            fields: Optional fields to return. Default is "*" to return all 
                fields.
            where: Optional where clause, defaults to '1=1'.
            add_params: Optional extra parameters to add to query string passed 
                as dict. Defaults to {}.
            records: Optional number of records to return.  Default is None to return all
                records within bounds of max record count unless exceed_limit is True.
            exceed_limit: option to get all records in layer. This option may be time consuming
                because the ArcGIS REST API uses default maxRecordCount of 1000, so queries
                must be performed in chunks to get all records.
            auto_save: Optional boolean arg to automatically apply edits when 
                using with statement, if True, will apply edits on the __exit__ 
                method. Defaults to True.
            useGlobalIds: (added at 10.4) Optional parameter which is false by 
                default. Requires the layer's supportsApplyEditsWithGlobalIds 
                property to be true.  When set to true, the features and attachments 
                in the adds, updates, deletes, and attachments parameters are 
                identified by their globalIds. When true, the service adds the 
                new features and attachments while preserving the globalIds 
                submitted in the payload. If the globalId of a feature 
                (or an attachment) collides with a preexisting feature 
                (or an attachment), that feature and/or attachment add fails. 
                Other adds, updates, or deletes are attempted if rollbackOnFailure
                is false. If rollbackOnFailure is true, the whole operation 
                fails and rolls back on any failure including a globalId collision.
                When useGlobalIds is true, updates and deletes are identified by 
                each feature or attachment globalId rather than their objectId 
                or attachmentId.
            kwargs: Any additional keyword arguments supported by the applyEdits method of the REST API, see
            http://resources.arcgis.com/en/help/arcgis:restapi/index.html#/Apply_Edits_Feature_Service_Layer/02r3000000r6000000/
        """
        
        layer = self
        class UpdateCursor(Cursor):
            """Class that updates a cursor."""

            def __init__(self,  feature_set, fieldOrder=[], auto_save=auto_save, useGlobalIds=useGlobalIds, **kwargs):
                """Inits class with cursor parameters.
                
                Args:
                    feature_set: Feature set as json or restapi.FeatureSet() object.
                    fieldOrder: List of order of fields for cursor row returns. 
                        Defaults to [].
                    auto_save: Optional boolean, determines whether autosave is enabled or not.
                    useGlobalIds: Optional boolean, when set to true, the features 
                        and attachments in the adds, updates, deletes, and 
                        attachments parameters are identified by their globalIds. 
                """
                
                super(UpdateCursor, self).__init__(feature_set, fieldOrder)
                self.useGlobalIds = useGlobalIds
                self._deletes = []
                self._updates = []
                self._feature_lookup_by_oid = {self._get_oid(ft): {'index': i, 'feature': ft} for i,ft in enumerate(self.features)}
                self._attachments = {
                    ADDS: [],
                    UPDATES: [],
                    DELETES: []
                }
                self._kwargs = {}
                for k,v in six.iteritems(kwargs):
                    if k not in('feature_set', 'fieldOrder', 'auto_save'):
                        self._kwargs[k] = v
                for i, f in enumerate(self.features):
                    ft = Feature(f)
                    oid = self._get_oid(ft)
                    self.features[i] = ft

            @property
            def has_oid(self):
                try:
                    return hasattr(self, OID_FIELD_NAME) and getattr(self, OID_FIELD_NAME)
                except:
                    return False

            @property
            def has_globalid(self):
                try:
                    return hasattr(self, GLOBALID_FIELD_NAME) and getattr(self, GLOBALID_FIELD_NAME)
                except:
                    return False

            @property
            def canEditByGlobalId(self):
                return all([
                    self.useGlobalIds,
                    layer.canUseGlobalIdsForEditing,
                    self.has_globalid,
                    getattr(self, GLOBALID_FIELD_NAME) in self.field_names
                ])

            def _find_by_oid(self, oid):
                """Gets a feature by its OID.
                
                Arg:
                    oid: Object ID.
                """
                for ft in iter(self.features):
                    if self._get_oid(ft) == oid:
                        return ft

            def _find_index_by_oid(self, oid):
                """Gets the index of a Feature by it's OID.
                
                Arg:
                    oid: Object ID.
                """
                return self._feature_lookup_by_oid.get(oid, {}).get('index')
                # for i, ft in enumerate(self.features):
                #     if self._get_oid(ft) == oid:
                #         return i

            def _replace_feature_with_oid(self, oid, feature):
                """Replaces a feature with OID with another Feature.
                
                Args:
                    oid: Object ID.
                    feature: The input feature.
                """

                feature = self._toJson(feature)
                if self._get_oid(feature) != oid:
                    feature.json[ATTRIBUTES][layer.OIDFieldName] = oid
                i = self._find_index_by_oid(oid)
                if i:
                    self.features[i] = feature
                # for i, ft in enumerate(self.features):
                #     if self._get_oid(ft) == oid:
                #         self.features[i] = feature

            def _find_by_globalid(self, globalid):
                """Returns a feature by its GlobalId.
                
                Arg:
                    globalid: The Global ID.
                """
                for ft in iter(self.features):
                    if self._get_globalid(ft) == globalid:
                        return ft

            def _find_index_by_globalid(self, globalid):
                """Returns the index of a Feature by it's GlobalId.
                
                Arg:
                    globalid: The Global ID.
                """
                for i, ft in enumerate(self.features):
                    if self._get_globalid(ft) == globalid:
                        return i

            def _replace_feature_with_globalid(self, globalid, feature):
                """Replaces a feature with GlobalId with another Feature.
                
                Args:
                    globalid: The Global ID.
                    feature: The input feature.
                """
                feature = self._toJson(feature)
                if self._get_globalid(feature) != globalid:
                    feature.json[ATTRIBUTES][layer.OIDFieldName] = globalid
                for i, ft in enumerate(self.features):
                    if self._get_globalid(ft) == globalid:
                        self.features[i] = feature

            def __enter__(self):
                return self

            def __exit__(self, type, value, traceback):
                if isinstance(type, Exception):
                    raise type(value)
                elif type is None and bool(auto_save):
                    self.applyEdits()

            def rows(self):
                """Returns Cursor.rows() as generator."""
                for feature in self.features:
                    yield list(self._createRow(feature, self.spatialReference).values)

            def _get_oid(self, row):
                """Returns the oid of a row.
                
                Arg:
                    row: The row to find the oid of.
                """

                if isinstance(row, six.integer_types):
                    return row
                try:
                    return self._toJson(row).get(layer.OIDFieldName)
                except:
                    return None

            def _get_globalid(self, row):
                """Returns the global ID of a row."""
                if isinstance(row, six.integer_types):
                    return row
                try:
                    return self._toJson(row).get(layer.GlobalIdFieldName or getattr(self, GLOBALID_FIELD_NAME))
                except:
                    return None

            def _get_row_identifier(self, row):
                """Returns the appropriate row identifier (OBJECTID or GlobalID)."""
                if self.canEditByGlobalId:
                    return self._get_globalid(row)
                return self._get_oid(row)

            def addAttachment(self, row_or_oid, attachment, **kwargs):
                """Adds an attachment.
                
                Args:
                    row_or_oid: Row returned from cursor or an OID/GlobalId.
                    attachment: Full path to attachment.

                Raises:
                    NotImplemented: '{} does not support attachments!'
                    ValueError: 'No OID field found! In order to add attachments, 
                        make sure the OID field is returned in the query.'
                    ValueError: 'No valid OID or GlobalId found to add attachment!'
                """

                if not hasattr(layer, HAS_ATTACHMENTS) or not getattr(layer, HAS_ATTACHMENTS):
                    raise NotImplemented('{} does not support attachments!'.format(layer))
                if not self.has_oid:
                    raise ValueError('No OID field found! In order to add attachments, make sure the OID field is returned in the query.')
##                # cannot get this to work :(
##                if layer.canApplyEditsWithAttachments:
##                    att_key = DATA
##                    if isinstance(attachment, six.string_types) and attachment.startswith('{'):
##                        # this is an upload id?
##                        att_key = UPLOAD_ID
##                    att = {
##                        PARENT_GLOBALID: self._get_globalid(row_or_oid),
##                        att_key: attachment
##                    }
##                    self._attachments[ADDS].append(att)
##                    return

                oid = self._get_oid(row_or_oid)
                if oid:
                    return layer.addAttachment(oid, attachment, **kwargs)
                raise ValueError('No valid OID or GlobalId found to add attachment!')

            def updateAttachment(self, row_or_oid, attachmentId, attachment, **kwargs):
                """Updates an attachment.

                Args:
                    row_or_oid: Row returned from cursor or an OID/GlobalId
                    attachment: Full path to attachment
                    attachmentId: ID of the attachment.
                
                Raises:
                    ValueError: 'No OID field found! In order to add attachments, 
                        make sure the OID field is returned in the query.'
                    ValueError: 'No valid OID or GlobalId found to add attachment!'
                """

                if not hasattr(layer, HAS_ATTACHMENTS) or not getattr(layer, HAS_ATTACHMENTS):
                    raise NotImplemented('{} does not support attachments!'.format(layer))
                if not self.has_oid:
                    raise ValueError('No OID field found! In order to add attachments, make sure the OID field is returned in the query.')
##                # cannot get this to work :(
##                if layer.canApplyEditsWithAttachments:
##                    att_key = DATA
##                    if isinstance(attachment, six.string_types) and attachment.startswith('{'):
##                        # this is an upload id?
##                        att_key = UPLOAD_ID
##                    att = {
##                        PARENT_GLOBALID: self._get_globalid(row_or_oid),
##                        GLOBALID_CAMEL: attachmentId,
##                        att_key: attachment
##                    }
##                    self._attachments[UPDATES].append(att)
##                    return

                oid = self._get_oid(row_or_oid)
                if oid:
                    return layer.updateAttachment(oid, attachmentId, attachment, **kwargs)
                raise ValueError('No valid OID or GlobalId found to add attachment!')

            def deleteAttachments(self, row_or_oid, attachmentIds, **kwargs):
                """Deletes an attachment.
                
                Args:
                    row_or_oid: Row returned from cursor or an OID/GlobalId.
                    attachment: Full path to attachment.
                    attachmentIds: ID's for the attachment.
                
                Raises:
                    ValueError: 'No OID field found! In order to add attachments, 
                        make sure the OID field is returned in the query.'
                    ValueError: 'No valid OID or GlobalId found to add attachment!'
                """

                if not hasattr(layer, HAS_ATTACHMENTS) or not getattr(layer, HAS_ATTACHMENTS):
                    raise NotImplemented('{} does not support attachments!'.format(layer))
                if not self.has_oid:
                    raise ValueError('No OID field found! In order to add attachments, make sure the OID field is returned in the query.')

                oid = self._get_oid(row_or_oid)
                if oid:
                    return layer.deleteAttachments(oid, attachmentIds, **kwargs)
                raise ValueError('No valid OID or GlobalId found to add attachment!')

            def updateRow(self, row):
                """Updates the feature with values from updated row.  If not used 
                        in context of a "with" statement, updates will have to be 
                        applied manually after all edits are made using the 
                        UpdateCursor.applyEdits() method.  When used in the 
                        context of a "with" statement, edits are automatically 
                        applied on __exit__.

                Arg:
                    row: List/tuple/Feature/Row that has been updated.
                """

                row = self._toJson(row)
                if self.canEditByGlobalId:
                    globalid = self._get_globalid(row)
                    self._replace_feature_with_globalid(globalid, row)
                else:
                    oid = self._get_oid(row)
                    self._replace_feature_with_oid(oid, row)
                self._updates.append(row)

            def deleteRow(self, row):
                """Deletes the row.

                Arg:
                    row: List/tuple/Feature/Row that has been updated.
                """

                oid = self._get_oid(row)
                self.features.remove(self._find_by_oid(oid))
                if oid:
                    self._deletes.append(oid)

            def applyEdits(self):
                """Applies edits to a layer."""
                attCount = filter(None, [len(atts) for op, atts in six.iteritems(self._attachments)])
                if (self.has_oid or (self.has_globalid and layer.canUseGlobalIdsForEditing and self.useGlobalIds)) \
                and any([self._updates, self._deletes, attCount]):
                    kwargs = {UPDATES: self._updates, DELETES: self._deletes}
                    if layer.canApplyEditsWithAttachments and self._attachments:
                        kwargs[ATTACHMENTS] = self._attachments
                    for k,v in six.iteritems(self._kwargs):
                        kwargs[k] = v
                    if layer.canUseGlobalIdsForEditing:
                        kwargs[USE_GLOBALIDS] = self.useGlobalIds
                    return layer.applyEdits(**kwargs)
                elif not (self.has_oid or not (self.has_globalid and layer.canUseGlobalIdsForEditing and self.useGlobalIds)):
                    raise RuntimeError('Missing OID or GlobalId Field in Data!')

        cur_fields = self._fix_fields(fields)
        add_params[F] = JSON
        fs = self.query(where, cur_fields, add_params, records, exceed_limit)
        return UpdateCursor(fs, fields)

    def insertCursor(self, fields=[], template_name=None, auto_save=True):
        """Inserts new features into layer using a cursor, , the applyEdits() 
                method is automatically called when used in a "with" statement 
                and auto_save is True.
        
        Args:
            fields: List of fields for cursor.
            template_name: Optional name of template from type. Defaults to None.
            auto_save: Optional boolean, automatically apply edits when using 
                with statement, if True, will apply edits on the __exit__ method.
        """

        layer = self
        field_names = [f.name for f in layer.fields if f.type not in (GLOBALID, OID)]
        class InsertCursor(object):
            """Class that inserts cursor."""
            def __init__(self, fields, template_name=None, auto_save=True):
                self._adds = []
                self.fields = fields
                self.has_geometry = getattr(layer, TYPE) == FEATURE_LAYER
                skip = (SHAPE_TOKEN, OID_TOKEN, layer.OIDFieldName)
                if template_name:
                    try:
                        self.template = self.get_template(template_name).templates[0].prototype
                    except:
                        self.template = {ATTRIBUTES: {k: NULL for k in self.fields if k not in skip}}
                else:
                    self.template = {ATTRIBUTES: {k: NULL for k in self.fields if k not in skip}}
                if self.has_geometry:
                    self.template[GEOMETRY] = NULL
                try:
                    self.geometry_index = self.fields.index(SHAPE_TOKEN)
                except ValueError:
                    try:
                        self.geometry_index = self.fields.index(layer.shapeFieldName)
                    except ValueError:
                        self.geometry_index = None

            def insertRow(self, row):
                """Inserts a row into the InsertCursor._adds cache.
                
                Args:
                    row: List/tuple/dict/Feature/Row that has been updated.
                """

                feature = {k:v for k,v in six.iteritems(self.template)}
                if isinstance(row, (list, tuple)):
                    for i, value in enumerate(row):
                        try:
                            field = self.fields[i]
                            if field == SHAPE_TOKEN:
                                feature[GEOMETRY] = Geometry(value).json
                            else:
                                if isinstance(value, datetime.datetime):
                                    value = date_to_mil(value)
                                feature[ATTRIBUTES][field] = value
                        except IndexError:
                            pass
                    self._adds.append(feature)
                    return
                elif isinstance(row, Feature):
                    row = row.json
                if isinstance(row, dict):
                    if GEOMETRY in row and self.has_geometry:
                        feature[GEOMETRY] = Geometry(row[GEOMETRY]).json
                    if ATTRIBUTES in row:
                        for f, value in six.iteritems(row[ATTRIBUTES]):
                            if f in feature[ATTRIBUTES]:
                                if isinstance(value, datetime.datetime):
                                    value = date_to_mil(value)
                                feature[ATTRIBUTES][f] = value
                    else:
                        for f, value in six.iteritems(row):
                            if f in feature[ATTRIBUTES]:
                                if isinstance(value, datetime.datetime):
                                    value = date_to_mil(value)
                                feature[ATTRIBUTES][f] = value
                            elif f == SHAPE_TOKEN and self.has_geometry:
                                feature[GEOMETRY] = Geometry(value).json
                    self._adds.append(feature)
                    return

            def applyEdits(self):
                """Applies the edits to the layer."""
                return layer.applyEdits(adds=self._adds)

            def __enter__(self):
                return self

            def __exit__(self, type, value, traceback):
                if isinstance(type, Exception):
                    raise type(value)
                elif type is None and bool(auto_save):
                    self.applyEdits()

        return InsertCursor(fields, template_name, auto_save)

    @property
    def canUseGlobalIdsForEditing(self):
        """Will be true if the layer supports applying edits where globalid values
                provided by the client are used. In order for supportsApplyEditsWithGlobalIds
                to be true, layers must have a globalid column and have 
                isDataVersioned as false. Layers with hasAttachments as true 
                additionally require attachments with globalids
                and attachments related to features via the features globalid.
        """
        return all([
            self.compatible_with_version(10.4),
            hasattr(self, SUPPORTS_APPLY_EDITS_WITH_GLOBALIDS),
            getattr(self, SUPPORTS_APPLY_EDITS_WITH_GLOBALIDS)
        ])


    @property
    def canApplyEditsWithAttachments(self):
        """Convenience property to check if attachments can be edited in
        applyEdits() method."""
        try:
            return all([
                self.compatible_with_version(10.4),
                hasattr(self, HAS_ATTACHMENTS),
                getattr(self, HAS_ATTACHMENTS)
            ])
        except AttributeError:
            return False

    @staticmethod
    def guess_content_type(attachment, content_type=''):
        # use mimetypes to guess "content_type"
        if not content_type:
            content_type = mimetypes.guess_type(os.path.basename(attachment))[0]
            if not content_type:
                known = mimetypes.types_map
                common = mimetypes.common_types
                ext = os.path.splitext(attachment)[-1].lower()
                if ext in known:
                    content_type = known[ext]
                elif ext in common:
                    content_type = common[ext]
        return content_type

    def get_template(self, name=None):
        """Returns a template by name.

        Arg:
            name: Optional arg for name of template. Defaults to None.
        """
        type_names = [t.get(NAME) for t in self.json.get(TYPES, [])]
        if name in type_names:
            for t in self.json.get(TYPES, []):
                if name == t.get(NAME):
                    return t
        try:
            return self.json.get(TYPES)[0]
        except IndexError:
            return {}


    def addFeatures(self, features, gdbVersion='', rollbackOnFailure=True):
        """Adds new features to feature service layer.

        Args:
            features: Esri JSON representation of features.
            gdbVersion: Optional arg for geodatabase version, defaults to ''.
            rollbackOnFailure: Optional boolean that determines if feature is 
                rolled back if method fails. Defaults to True.

        ex:
        adds = [{"geometry":
                     {"x":-10350208.415443439,
                      "y":5663994.806146532,
                      "spatialReference":
                          {"wkid":102100}},
                 "attributes":
                     {"Utility_Type":2,"Five_Yr_Plan":"No","Rating":None,"Inspection_Date":1429885595000}}]
        """

        add_url = self.url + '/addFeatures'
        if isinstance(features, (list, tuple)):
            features = json.dumps(features, ensure_ascii=False)
        params = {FEATURES: features,
                  GDB_VERSION: gdbVersion,
                  ROLLBACK_ON_FAILURE: rollbackOnFailure,
                  F: JSON}

        # add features
        return self.__edit_handler(self.request(add_url, params))

    def updateFeatures(self, features, gdbVersion='', rollbackOnFailure=True):
        """Updates features in feature service layer.
        
        Args:
            features: Features to be updated (JSON).
            Args:*
            gdbVersion: Optional geodatabase version to apply edits. Defaults to ''.
            rollbackOnFailure: Optional boolean, specifies if the edits should be
                applied only if all submitted edits succeed
        
        # example syntax
        updates = [{"geometry":
            {"x"::10350208.415443439,
            "y":5663994.806146532,
            "spatialReference":
            {"wkid":102100}},
            "attributes":
            {"Five_Yr_Plan":"Yes","Rating":90,"OBJECTID":1}}] #only fields that were changed!
        """
        
        if isinstance(features, (list, tuple)):
            features = json.dumps(features, ensure_ascii=False)
        update_url = self.url + '/updateFeatures'
        params = {FEATURES: features,
                  GDB_VERSION: gdbVersion,
                  ROLLBACK_ON_FAILURE: rollbackOnFailure,
                  F: JSON}

        # update features
        return self.__edit_handler(self.request(update_url, params))

    def deleteFeatures(self, oids='', where='', geometry='', geometryType='',
                       spatialRel='', inSR='', gdbVersion='', rollbackOnFailure=True):
        """Deletes features based on list of OIDs.
        
        Args:
            oids: Optional list of oids or comma separated values. Defaults to ''.
            where: Optional where clause for features to be deleted.  All selected 
                features will be deleted. Defaults to ''.
            geometry: Optional geometry JSON object used to delete features. 
                Defaults to ''.
            geometryType: Optional type of geometry. Defaults to ''.
            spatialRel: Optional spatial relationship.  Defaults to ''.
            inSR: Optional input spatial reference for geometry. Defaults to ''.
            gdbVersion: Optional geodatabase version to apply edits. Defaults to ''.
            rollbackOnFailure: Optional specify if the edits should be applied 
                only if all submitted edits succeed. Defaults to True.
        
        oids format example:
            oids = [1, 2, 3] # list
            oids = "1, 2, 4" # as string
        """

        if not geometryType:
            geometryType = ESRI_ENVELOPE
        if not spatialRel:
            spatialRel = ESRI_INTERSECT

        del_url = self.url + '/deleteFeatures'
        if isinstance(oids, (list, tuple)):
            oids = ', '.join(map(str, oids))
        params = {OBJECT_IDS: oids,
                  WHERE: where,
                  GEOMETRY: geometry,
                  GEOMETRY_TYPE: geometryType,
                  SPATIAL_REL: spatialRel,
                  GDB_VERSION: gdbVersion,
                  ROLLBACK_ON_FAILURE: rollbackOnFailure,
                  F: JSON}

        # delete features
        return self.__edit_handler(self.request(del_url, params))

    def applyEdits(self, adds=None, updates=None, deletes=None, attachments=None, gdbVersion=None, rollbackOnFailure=TRUE, useGlobalIds=False, **kwargs):
        """Applies edits on a feature service layer.
        
        Args:
            adds: Optional features to add (JSON). Defaults to None.
            updates: Optional features to be updated (JSON). Defaults to None.
            deletes: Optional oids to be deleted 
                (list, tuple, or comma separated string). Defaults to None.
            attachments: Optional attachments to be added, updated or deleted 
                (added at version 10.4). Attachments in this instance must use 
                global IDs and the layer's "supportsApplyEditsWithGlobalIds" must 
                be true. Defaults to None.
            gdbVersion: Optional geodatabase version to apply edits.
                Defaults to None.
            rollbackOnFailure: Optional boolean to specify if the edits should be 
                applied only if all submitted edits succeed. Defaults to True.
            useGlobalIds: (added at 10.4) Optional parameter which is false by 
                default. Requires the layer's supportsApplyEditsWithGlobalIds 
                property to be true.  When set to true, the features and 
                attachments in the adds, updates, deletes, and attachments 
                parameters are identified by their globalIds. When true, the 
                service adds the new features and attachments while preserving 
                the globalIds submitted in the payload. If the globalId of a feature
                (or an attachment) collides with a preexisting feature 
                (or an attachment), that feature and/or attachment add fails.
                Other adds, updates, or deletes are attempted if rollbackOnFailure
                is false. If rollbackOnFailure is true, the whole operation 
                fails and rolls back on any failure including a globalId collision.
                When useGlobalIds is true, updates and deletes are identified by 
                each feature or attachment globalId rather than their objectId or 
                attachmentId.
            kwargs: Any additional keyword arguments supported by the applyEdits 
                method of the REST API, see
                http://resources.arcgis.com/en/help/arcgis:restapi/index.html#/Apply_Edits_Feature_Service_Layer/02r3000000r6000000/
            
            attachments example (supported only in 10.4 and above):
            {
            "adds": [{
            "globalId": "{55E85F98:FBDD4129:9F0B848DD40BD911}",
            "parentGlobalId": "{02041AEF:41744d81:8A98D7AC5B9F4C2F}",
            "contentType": "image/pjpeg",
            "name": "Pothole.jpg",
            "uploadId": "{DD1D0A30:CD6E4ad7:A516C2468FD95E5E}"
            },
            {
            "globalId": "{3373EE9A:461941B7:918BDB54575465BB}",
            "parentGlobalId": "{6FA4AA68:76D84856:971DB91468FCF7B7}",
            "contentType": "image/pjpeg",
            "name": "Debree.jpg",
            "data": "<base 64 encoded data>"
            }
            ],
            "updates": [{
            "globalId": "{8FDD9AEF:E05E440A:94261D7F301E1EBA}",
            "contentType": "image/pjpeg",
            "name": "IllegalParking.jpg",
            "uploadId": "{57860BE4:3B8544DD:A0E7BE252AC79061}"
            }],
            "deletes": [
            "{95059311:741C4596:88EFC437C50F7C00}",
            " {18F43B1C:27544D05:BCB0C4643C331C29}"
            ]
            }
        """

        edits_url = self.url + '/applyEdits'
        if isinstance(adds, FeatureSet):
            adds = json.dumps(adds.features, ensure_ascii=False, cls=RestapiEncoder)
        elif isinstance(adds, (list, tuple)):
            adds = json.dumps(adds, ensure_ascii=False, cls=RestapiEncoder)
        if isinstance(updates, FeatureSet):
            updates = json.dumps(updates.features, ensure_ascii=False, cls=RestapiEncoder)
        elif isinstance(updates, (list, tuple)):
            updates = json.dumps(updates, ensure_ascii=False, cls=RestapiEncoder)
        if isinstance(deletes, (list, tuple)):
            deletes = ', '.join(map(str, deletes))

        params = {ADDS: adds,
                  UPDATES: updates,
                  DELETES: deletes,
                  GDB_VERSION: gdbVersion,
                  ROLLBACK_ON_FAILURE: rollbackOnFailure,
                  USE_GLOBALIDS: useGlobalIds
        }

        # handle attachment edits (added at version 10.4) cannot get this to work :(
##        if self.canApplyEditsWithAttachments and isinstance(attachments, dict):
##            for edit_type in (ADDS, UPDATES):
##                if edit_type in attachments:
##                    for att in attachments[edit_type]:
##                        if att.get(DATA) and os.path.isfile(att.get(DATA)):
##                            # multipart encoded files
##                            ct = self.guess_content_type(att.get(DATA))
##                            if CONTENT_TYPE not in att:
##                                att[CONTENT_TYPE] = ct
##                            if NAME not in att:
##                                att[NAME] = os.path.basename(att.get(DATA))
##                            with open(att.get(DATA), 'rb') as f:
##                                att[DATA] = 'data:{};base64,'.format(ct) + base64.b64encode(f.read())
##                                print(att[DATA][:50])
##                            if GLOBALID_CAMEL not in att:
##                                att[GLOBALID_CAMEL] = 'f5e0f368-17a1-4062-b848-48eee2dee1d5'
##                        temp = {k:v for k,v in six.iteritems(att) if k != 'data'}
##                        temp[DATA] = att['data'][:50]
##                        print(json.dumps(temp, indent=2))
##            params[ATTACHMENTS] = attachments
##            if any([params[ATTACHMENTS].get(k) for k in (ADDS, UPDATES, DELETES)]):
##                params[USE_GLOBALIDS] = TRUE
        # add other keyword arguments
        for k,v in six.iteritems(kwargs):
            kwargs[k] = v
        return self.__edit_handler(self.request(edits_url, params))

    def addAttachment(self, oid, attachment, content_type='', gdbVersion=''):
        """Adds an attachment to a feature service layer.
        
        Args:
            oid: OBJECT ID of feature in which to add attachment.
            attachment: Path to attachment.
            content_type: Optional html media type for "content_type" header.  
                If nothing provided, will use a best guess based on file extension 
                (using mimetypes). Defaults to ''.
            gdbVersion: Optional geodatabase version for attachment. Defaults to ''.
        
        Raises:
            NotImplementedError: 'FeatureLayer "{}" does not support attachments!'
            
        Valid content types can be found here @:
            http://en.wikipedia.org/wiki/Internet_media_type
        """
        if self.hasAttachments:

            content_type = self.guess_content_type(attachment, content_type)

            # make post request
            att_url = '{}/{}/addAttachment'.format(self.url, oid)
            files = {ATTACHMENT: (os.path.basename(attachment), open(attachment, 'rb'), content_type)}
            params = {F: JSON}
            if isinstance(self.token, Token) and self.token.isAGOL:
                params[TOKEN] = str(self.token)
            if gdbVersion:
                params[GDB_VERSION] = gdbVersion
            return self.__edit_handler(requests.post(att_url, params, files=files, cookies=self._cookie, verify=False).json(), oid)

        else:
            raise NotImplementedError('FeatureLayer "{}" does not support attachments!'.format(self.name))

    def deleteAttachments(self, oid, attachmentIds, gdbVersion='', **kwargs):
        """Deletes attachments in a feature layer.
        
        Args:
            oid: OBJECT ID of feature in which to add attachment.
            attachmentIds: IDs of attachments to be deleted.  If attachmentIds 
                param is set to "All", all attachments for this feature will be 
                deleted.
            gdbVersion: Optional arg for geodatabase verison, defaults to ''.
            kwargs: Optional, additional keyword arguments supported by 
                deleteAttachments method.
        
        Raises:
            NotImplementedError: 'FeatureLayer "{}" does not support attachments!'
        """

        if self.hasAttachments:
            att_url = '{}/{}/deleteAttachments'.format(self.url, oid)
            if isinstance(attachmentIds, (list, tuple)):
                attachmentIds = ','.join(map(str, attachmentIds))
            elif isinstance(attachmentIds, six.string_types) and attachmentIds.title() == 'All':
                attachmentIds = ','.join(map(str, [getattr(att, ID) for att in self.attachments(oid)]))
            if not attachmentIds:
                return
            params = {F: JSON, ATTACHMENT_IDS: attachmentIds}
            if isinstance(self.token, Token) and self.token.isAGOL:
                params[TOKEN] = str(self.token)
            if gdbVersion:
                params[GDB_VERSION] = gdbVersion
            for k,v in six.iteritems(kwargs):
                params[k] = v
            return self.__edit_handler(requests.post(att_url, params, cookies=self._cookie, verify=False).json(), oid)
        else:
            raise NotImplementedError('FeatureLayer "{}" does not support attachments!'.format(self.name))

    def updateAttachment(self, oid, attachmentId, attachment, content_type='', gdbVersion='', validate=False):
        """Adds an attachment to a feature service layer.
        
        Args:
            oid: OBJECT ID of feature in which to add attachment.
            attachmentId: ID of feature attachment.
            attachment: Path to attachment.
            content_type: Optional html media type for "content_type" header. 
                If nothing provided, will use a best guess based on file 
                extension (using mimetypes). Defaults to ''.
            gdbVersion: Optional, geodatabase version for attachment. 
                Defaults to ''.
            validate: Optional boolean to check if attachment ID exists within 
                feature first before attempting an update, this adds a small 
                amount of overhead to method because a request to fetch attachments 
                is made prior to updating. Default is False.
        
        Raises:
            ValueError: 'Attachment with ID "{}" not found in Feature with OID "{}"'
            NotImplementedError: 'FeatureLayer "{}" does not support attachments!'

        valid content types can be found here @:
            http://en.wikipedia.org/wiki/Internet_media_type
        """
        if self.hasAttachments:
            content_type = self.guess_content_type(attachment, content_type)

            # make post request
            att_url = '{}/{}/updateAttachment'.format(self.url, oid)
            if validate:
                if attachmentId not in [getattr(att, ID) for att in self.attachments(oid)]:
                    raise ValueError('Attachment with ID "{}" not found in Feature with OID "{}"'.format(oid, attachmentId))
            files = {ATTACHMENT: (os.path.basename(attachment), open(attachment, 'rb'), content_type)}
            params = {F: JSON, ATTACHMENT_ID: attachmentId}
            if isinstance(self.token, Token) and self.token.isAGOL:
                params[TOKEN] = str(self.token)
            if gdbVersion:
                params[GDB_VERSION] = gdbVersion
            return self.__edit_handler(requests.post(att_url, params, files=files, cookies=self._cookie, verify=False).json(), oid)

        else:
            raise NotImplementedError('FeatureLayer "{}" does not support attachments!'.format(self.name))

    def calculate(self, exp, where='1=1', sqlFormat='standard'):
        """Calculates a field in a Feature Layer.
        
        Args:
            exp: Expression as JSON [{"field": "Street", "value": "Main St"},..].
            where: Optional where clause for field calculator. Defaults to '1=1'.
            sqlFormat: Optional SQL format for expression (standard|native). 
                Defaults to 'standard'.
        
        Raises:
            NotImplementedError: 'FeatureLayer "{}" does not support field calculations!'
        
        Example expressions as JSON:
            exp : [{"field" : "Quality", "value" : 3}]
            exp :[{"field" : "A", "sqlExpression" : "B*3"}]
        """

        if self.json.get(SUPPORTS_CALCULATE, False):
            calc_url = self.url + '/calculate'
            p = {WHERE: where,
                CALC_EXPRESSION: json.dumps(exp, ensure_ascii=False),
                SQL_FORMAT: sqlFormat}

            return self.request(calc_url, params=p)

        else:
            raise NotImplementedError('FeatureLayer "{}" does not support field calculations!'.format(self.name))

    def __edit_handler(self, response, feature_id=None):
        """Handler for edit results.

        response: Response from edit operation.
        feature_id: Optional ID for feature, defaults to None.
        """

        e = EditResult(response, feature_id)
        self.editResults.append(e)
        e.summary()
        return e

class FeatureTable(FeatureLayer, MapServiceTable):
    pass

class GeometryService(RESTEndpoint):
    """Class that handles the ArcGIS geometry service."""
    linear_units = sorted(projections.linearUnits.keys())
    _default_url = 'https://utility.arcgisonline.com/ArcGIS/rest/services/Geometry/GeometryServer'

    def __init__(self, url=None, usr=None, pw=None, token=None, proxy=None, referer=None):
        """Inits class with login info for arcgis geometry service.
        
        Args:
            url: Optional arg for url to service. Defaults to None.
            usr: Optional arg for username for service. Defaults to None.
            pw: Optional arg for password for service. Defaults to None.
            token: Optional arg for token for service, Defaults to None.
            proxy: Optional arg for proxy for service. Defaults to None.
            referer: Optional arg for referer. Defaults to None.
        """
        
        if not url:
            # use default arcgis online Geometry Service
            url = self._default_url
        super(GeometryService, self).__init__(url, usr, pw, token, proxy, referer)

    @staticmethod
    def getLinearUnits():
        """Returns a Munch() dictionary of linear units."""
        return projections.linearUnits

    @staticmethod
    def getLinearUnitWKID(unit_name):
        """Returns a well known ID from a unit name.

        Arg:
            unit_name: Name of unit to fetch WKID for. It is safe to use this as
                a filter to ensure a valid WKID is extracted.  if a WKID is passed in,
                that same value is returned.  This argument is expecting a string 
                from linear_units list.  Valid options can be viewed with 
                GeometryService.linear_units.
        """

        if isinstance(unit_name, int) or six.text_type(unit_name).isdigit():
            return int(unit_name)

        for k,v in six.iteritems(projections.linearUnits):
            if k.lower() == unit_name.lower():
                return int(v[WKID])

    @staticmethod
    def validateGeometries(geometries, use_envelopes=False):
        """Validates geometries to be passed into operations that use an
                array of geometries.

        Args:
            geometries: List of geometries. Valid inputs are GeometryCollection()'s, 
                json, FeatureSet()'s, or Geometry()'s.
            use_envelopes: Optional boolean, determines if method will use envelopes 
                of all the input geometries. Defaults to False.
        """

        return GeometryCollection(geometries, use_envelopes)

    @geometry_passthrough
    def buffer(self, geometries, distances, unit='', inSR=None, outSR='', use_envelopes=False, **kwargs):
        """Buffers a single geoemetry or multiple.

        Args:
            geometries: Array of geometries to be buffered. The spatial reference 
                of the geometries is specified by inSR. The structure of each 
                geometry in the array is the same as the structure of the JSON 
                geometry objects returned by the ArcGIS REST API.  This should be
                a restapi.GeometryCollection().
            distances: The distances that each of the input geometries is buffered. 
                The distance units are specified by unit.
            units: Optional input units (esriSRUnit_Meter|esriSRUnit_StatuteMile|esriSRUnit_Foot|esriSRUnit_Kilometer|
                esriSRUnit_NauticalMile|esriSRUnit_USNauticalMile). Defaults to ''.
            inSR: Optional wkid for input geometry. Default is None.
            outSR: Optional wkid for output geometry. Default is ''.
            use_envelopes: Optional arg, not a valid option in ArcGIS REST API, 
                this is an extra argument that will convert the geometries to 
                bounding box envelopes ONLY IF they are restapi.Geometry objects,
                otherwise this arg is ignored.

        restapi constants for units:
            restapi.ESRI_METER
            restapi.ESRI_MILE
            restapi.ESRI_FOOT
            restapi.ESRI_KILOMETER
            restapi.ESRI_NAUTICAL_MILE
            restapi.ESRI_US_NAUTICAL_MILE
        """

        buff_url = self.url + '/buffer'
        geometries = self.validateGeometries(geometries)
        params = {F: PJSON,
                  GEOMETRIES: geometries,
                  IN_SR: inSR or geometries.getSR(),
                  DISTANCES: distances,
                  UNIT: self.getLinearUnitWKID(unit) or unit,
                  OUT_SR: outSR,
                  UNION_RESULTS: FALSE,
                  GEODESIC: TRUE,
                  OUT_SR: None,
                  BUFFER_SR: None
        }

        # add kwargs
        for k,v in six.iteritems(kwargs):
            if k not in (GEOMETRIES, DISTANCES, UNIT):
                params[k] = v

        # perform operation
        print('params: {}'.format({k:v for k,v in six.iteritems(params) if k != GEOMETRIES}))
        return GeometryCollection(self.request(buff_url, params),
                                  spatialReference=outSR if outSR else inSR)

    @geometry_passthrough
    def intersect(self, geometries, geometry, sr):
        """Performs intersection of input geometries and other geometry.

        Args:
            geometries: Input geometries 
                (GeometryCollection|FeatureSet|json|arcpy.mapping.Layer|FeatureClass|Shapefile).
            geometry: Other geometry to intersect.
            sr: Optional spatial reference for input geometries, if not specified 
                will be derived from input geometries.
        """

        query_url = self.url + '/intersect'
        geometries = self.validateGeometries(geometries)
        sr = sr or geometries.getWKID() or NULL
        params = {
            GEOMETRY: geometry,
            GEOMETRIES: geometries,
            SR: sr
        }
        return GeometryCollection(self.request(query_url, params), spatialReference=sr)

    def union(self, geometries, sr=None):
        """Performs union of input geometries.
        
        Args:
            geometries: Input geometries 
                (GeometryCollection|FeatureSet|json|arcpy.mapping.Layer|FeatureClass|Shapefile).
            sr: Optional spatial reference for input geometries, if not specified 
                will be derived from input geometries.
        """

        url = self.url + '/union'
        geometries = self.validateGeometries(geometries)
        sr = sr or geometries.getWKID() or NULL
        params = {
            GEOMETRY: geometries,
            GEOMETRIES: geometries,
            SR: sr
        }
        return Geometry(self.request(url, params), spatialReference=sr)

    def findTransformations(self, inSR, outSR, extentOfInterest='', numOfResults=1):
        """Finds and returns the most applicable transformation based on inSR and 
                outSR.
        
        Args:
            inSR: Input Spatial Reference (wkid).
            outSR: Output Spatial Reference (wkid).
            Args:*
            extentOfInterest: Optional bounding box of the area of interest 
                specified as a JSON envelope. If provided, the extent of 
                interest is used to return the most applicable geographic 
                transformations for the area. If a spatial reference is 
                not included in the JSON envelope, the inSR is used for the
                envelope. Defaults to ''.
            numOfResults: The number of geographic transformations to return. The
                default value is 1. If numOfResults has a value of 1, all applicable
                transformations are returned.

        return looks like this:
            [
              {
                "wkid": 15851,
                "latestWkid": 15851,
                "name": "NAD_1927_To_WGS_1984_79_CONUS"
              },
              {
                "wkid": 8072,
                "latestWkid": 1172,
                "name": "NAD_1927_To_WGS_1984_3"
              },
              {
                "geoTransforms": [
                  {
                    "wkid": 108001,
                    "latestWkid": 1241,
                    "transformForward": true,
                    "name": "NAD_1927_To_NAD_1983_NADCON"
                  },
                  {
                    "wkid": 108190,
                    "latestWkid": 108190,
                    "transformForward": false,
                    "name": "WGS_1984_(ITRF00)_To_NAD_1983"
                  }
                ]
              }
            ]
        """

        params = {IN_SR: inSR,
                  OUT_SR: outSR,
                  EXTENT_OF_INTEREST: extentOfInterest,
                  NUM_OF_RESULTS: numOfResults
                }

        res = self.request(self.url + '/findTransformations', params)
        if int(numOfResults) == 1:
            return res[0]
        else:
            return res

    @geometry_passthrough
    def project(self, geometries, inSR, outSR, transformation='', transformForward='false'):
        """Projects a single or group of geometries.
        
        Args:
            geometries: Input geometries to project.
            inSR: Input spatial reference.
            outSR: Output spatial reference.
            transformation: Optional arg for transformation. Defaults to ''.
            trasnformForward: Optional boolean arg that determines if projection 
                is transformed, default is False.
        """

        params = {GEOMETRIES: self.validateGeometries(geometries),
                  IN_SR: inSR,
                  OUT_SR: outSR,
                  TRANSFORMATION: transformation,
                  TRANSFORM_FORWARD: transformForward
                }

        return GeometryCollection(self.request(self.url + '/project', params),
                                spatialReference=outSR if outSR else inSR)

    def __repr__(self):
        try:
            return "<restapi.GeometryService: '{}'>".format(self.url.split('://')[1].split('/')[0])
        except:
            return '<restapi.GeometryService>'

class ImageService(BaseService):
    geometry_service = None

    def adjustbbox(self, boundingBox):
        """Method to adjust bounding box for image clipping to maintain
                cell size.

        Arg:
            boundingBox: Bounding box string (comma separated).
        """

        cell_size = int(self.pixelSizeX)
        if isinstance(boundingBox, six.string_types):
            boundingBox = boundingBox.split(',')
        return ','.join(map(str, map(lambda x: Round(x, cell_size), boundingBox)))

    def pointIdentify(self, geometry=None, **kwargs):
        """Method to get pixel value from x,y coordinates or JSON point object.

        Arg:
            geometry: Input restapi.Geometry() object or point as json. Defaults to None.

        Recognized key word arguments:
            x: x coordinate
            y: y coordinate
            inSR: Input spatial reference.  Should be supplied if spatial
                reference is different from the Image Service's projection.

        geometry example:
            geometry = {"x":3.0,"y":5.0,"spatialReference":{"wkid":102100}}
        
        Returns:
            Pixel value.
        """
        IDurl = self.url + '/identify'

##        if geometry is not None:
##            if not isinstance(geometry, Geometry):
##                geometry = Geometry(geometry)
##
##        elif GEOMETRY in kwargs:
##            g = Geometry(kwargs[GEOMETRY])
##
##        elif X in kwargs and Y in kwargs:
##            g = {X: kwargs[X], Y: kwargs[Y]}
##            if SR in kwargs:
##                g[SPATIAL_REFERENCE] = {WKID: kwargs[SR]}
##            elif SPATIAL_REFERENCE in kwargs:
##                g[SPATIAL_REFERENCE] = {WKID: kwargs[SPATIAL_REFERENCE]}
##            else:
##                g[SPATIAL_REFERENCE] = {WKID: self.getSR()}
##            geometry = Geometry(g)
##
##        else:
##            raise ValueError('Not a valid input geometry!')

        params = {
            GEOMETRY: geometry.dumps(),
            GEOMETRY_TYPE: geometry.geometryType,
            F: JSON,
            RETURN_GEOMETRY: FALSE,
            RETURN_CATALOG_ITEMS: FALSE,
        }

        for k,v in six.iteritems(kwargs):
            if k not in params:
                params[k] = v

        j = self.request(IDurl, params)
        return j.get(VALUE)

    def exportImage(self, poly, out_raster, envelope=False, rendering_rule=None, interp=BILINEAR_INTERPOLATION, nodata=None, **kwargs):
        """Method to export an AOI from an Image Service.
        
        Args:
            poly: Polygon features.
            out_raster: Output raster image.
            envelope: Optional boolean to use envelope of polygon, 
                defaults to False.
            rendering_rule: Optional rendering rule to perform raster functions 
                as JSON. Defaults to None.
            interp: Optional arg for interpolation, defaults to 
                BILINEAR_INTERPOLATION.
            nodata: Optional argument, defaults to None.
            kwargs: Optional key word arguments for other arguments.
        """

        if not out_raster.endswith('.tif'):
            out_raster = os.path.splitext(out_raster)[0] + '.tif'
        query_url = '/'.join([self.url, EXPORT_IMAGE])

        if isinstance(poly, Geometry):
            in_geom = poly
        else:
            in_geom = Geometry(poly)

        sr = in_geom.getSR()

        if envelope:
            geojson = in_geom.envelope()
            geometryType = ESRI_ENVELOPE
        else:
            geojson = in_geom.dumps()
            geometryType = in_geom.geometryType

##        if sr != self.spatialReference:
##            self.geometry_service = GeometryService()
##            gc = self.geometry_service.project(in_geom, in_geom.spatialReference, self.getSR())
##            in_geom = gc

        # The adjust aspect ratio doesn't seem to fix the clean pixel issue
        bbox = self.adjustbbox(in_geom.envelope())
        #if not self.compatible_with_version(10.3):
        #    bbox = self.adjustbbox(in_geom.envelope())
        #else:
        #    bbox = in_geom.envelope()

        # imageSR
        if IMAGE_SR not in kwargs:
            imageSR = sr
        else:
            imageSR = kwargs[IMAGE_SR]

        if NO_DATA in kwargs:
            nodata = kwargs[NO_DATA]

        # check for raster function availability
        if not self.allowRasterFunction:
            rendering_rule = None

        # find width and height for image size (round to whole number)
##        bbox_int = map(int, map(float, bbox.split(',')))
##        width = abs(bbox_int[0] - bbox_int[2])
##        height = abs(bbox_int[1] - bbox_int[3])
        bbox_int = map(int, map(float, bbox.split(',')))
        width = abs(bbox_int[0] - bbox_int[2]) / int(self.get(PIXEL_SIZE_X))
        height = abs(bbox_int[1] - bbox_int[3]) / int(self.get(PIXEL_SIZE_Y))

        matchType = NO_DATA_MATCH_ANY
        if ',' in str(nodata):
            matchType = NO_DATA_MATCH_ALL

        # set params
        p = {F: PJSON,
             RENDERING_RULE: rendering_rule,
             ADJUST_ASPECT_RATIO: TRUE,
             BBOX: bbox,
             FORMAT: TIFF,
             IMAGE_SR: imageSR,
             BBOX_SR: sr,
             SIZE: '{0},{1}'.format(width,height),
             PIXEL_TYPE: self.pixelType,
             NO_DATA_INTERPRETATION: '&'.join(map(str, filter(lambda x: x not in (None, ''),
                ['noData=' + str(nodata) if nodata != None else '', 'noDataInterpretation=%s' %matchType if nodata != None else matchType]))),
             INTERPOLATION: interp
            }

        # overwrite with kwargs
        for k,v in six.iteritems(kwargs):
            if k not in [SIZE, BBOX_SR]:
                p[k] = v

        # post request
        r = self.request(query_url, p)

        if r.get('href', None) is not None:
            tiff = self.request(r.get('href').strip(), ret_json=False).content
            with open(out_raster, 'wb') as f:
                f.write(tiff)
            print('Created: "{0}"'.format(out_raster))

    def clip(self, poly, out_raster, envelope=True, imageSR='', noData=None):
        """Method to clip a raster.
        
        Args:
            poly: Polygon.
            out_raster: Output raster.
            envelope: Optional boolean to use bounding box, defaults to True.
            imageSR: Optional arg for spatial reference image, defaults to ''.
            noData: Optional, defaults to None.
        """

        # check for raster function availability
        if not self.allowRasterFunction:
            raise NotImplemented('This Service does not support Raster Functions!')
        if envelope:
            if not isinstance(poly, Geometry):
                poly = Geometry(poly)
            geojson = poly.envelopeAsJSON()
            geojson[SPATIAL_REFERENCE] = poly.json[SPATIAL_REFERENCE]
        else:
            geojson = Geometry(poly).dumps() if not isinstance(poly, Geometry) else poly.dumps()
        ren = {
          "rasterFunction" : "Clip",
          "rasterFunctionArguments" : {
            "ClippingGeometry" : geojson,
            "ClippingType": CLIP_INSIDE,
            },
          "variableName" : "Raster"
        }
        self.exportImage(poly, out_raster, rendering_rule=ren, noData=noData, imageSR=imageSR)

    def arithmetic(self, poly, out_raster, raster_or_constant, operation=RASTER_MULTIPLY, envelope=False, imageSR='', **kwargs):
        """Performs arithmetic operations against a raster.
        
        Args:
            poly: Input polygon or JSON polygon object.
            out_raster: Full path to output raster.
            raster_or_constant: Raster to perform opertion against or constant value.
            operation: Optional arithmetic operation to use, default is multiply 
                (3) (RASTER_MULTIPLY) all options: (1|2|3). 
            envelope: Optional boolean, if true, will use bounding box of 
                input features.
            imageSR: Optional output image spatial reference.
        
        Operations:
        1 : esriRasterPlus
        2 : esriRasterMinus
        3 : esriRasterMultiply
        """

        ren = {
              "rasterFunction" : "Arithmetic",
              "rasterFunctionArguments" : {
                   "Raster" : "$$",
                   "Raster2": raster_or_constant,
                   "Operation" : operation
                 }
              }
        self.exportImage(poly, out_raster, rendering_rule=json.dumps(ren, ensure_ascii=False), imageSR=imageSR, **kwargs)

class GPService(BaseService):
    """GP Service object.
    
    Args:
        url: GP service url
    Below args only required if security is enabled:
        usr: Username credentials for ArcGIS Server.
        pw: Password credentials for ArcGIS Server.
        token: Token to handle security (alternative to usr and pw).
        proxy: Optional boolean to use proxy page to handle security, need to 
            provide full path to proxy url. Defaults to None.
    """

    def task(self, name):
        """Returns a GP Task object.
        
        Arg:
            name: Name of task.
        """
        return GPTask('/'.join([self.url, name]))

class GPTask(BaseService):
    """GP Service object.
    
    Args:
        url: GP service url
    Below args only required if security is enabled:
        usr: Username credentials for ArcGIS Server.
        pw: Password credentials for ArcGIS Server.
        token: Token to handle security (alternative to usr and pw).
        proxy: Optional boolean to use proxy page to handle security, need to 
            provide full path to proxy url. Defaults to None.
    """

    @property
    def isSynchronous(self):
        """Task is synchronous."""
        return self.executionType == SYNCHRONOUS

    @property
    def isAsynchronous(self):
        """Task is asynchronous."""
        return self.executionType == ASYNCHRONOUS

    @property
    def outputParameter(self):
        """Returns the first output parameter (if there is one)."""
        try:
            return self.outputParameters[0]
        except IndexError:
            return None

    @property
    def outputParameters(self):
        """Returns list of all output parameters."""
        return [p for p in self.parameters if p.direction == OUTPUT_PARAMETER]


    def list_parameters(self):
        """Lists the parameter names."""
        return [p.name for p in self.parameters]

    def check_job_status(self, jobId):
        jobs_url = '{}/jobs/{}'.format(self.url, jobId)
        return GPJob(self.request(jobs_url, { F: JSON }))

    def run(self, params_json={}, outSR='', processSR='', returnZ=False, returnM=False, wait=True, timeout=1000, **kwargs):
        """Runs a Syncrhonous/Asynchronous GP task, automatically uses appropriate 
                option.

        Args:
            task: Name of task to run.
            params_json: JSON object with {parameter_name: value, param2: value2, ...}.
            outSR: Optional spatial reference for output geometries. Defaults to ''.
            processSR: Optional spatial reference used for geometry opterations. 
                Defaults to ''.
            returnZ: Optional boolean to return Z values with data if applicable. 
                Defaults to False.
            returnM: Optional boolean to return M values with data if applicable. 
                Defaults to False.
            wait (bool): option to wait for completion.  Only applicable when running
                asynchronous jobs
            kwargs: Keyword arguments, can substitute this to pass in GP params 
                by name instead of using the params_json dictionary. Only valid 
                if params_json dictionary is not supplied.
        """
        
        if self.isSynchronous:
            runType = EXECUTE
        else:
            runType = SUBMIT_JOB
        gp_exe_url = '/'.join([self.url, runType])
        if not params_json:
            params_json = {}
            for k,v in six.iteritems(kwargs):
                params_json[k] = v
        params_json['env:outSR'] = outSR
        params_json['env:processSR'] = processSR
        params_json[RETURN_Z] = returnZ
        params_json[RETURN_M] = returnZ
        params_json[F] = JSON
        start = datetime.datetime.now()

        r = self.request(gp_exe_url, params_json, ret_json=False)

        # get result object as JSON
        res = r.json()

        if self.isAsynchronous:
            if not wait:
                # return job id now
                return GPJob(res)

            # get status of job
            time.sleep(1)
            res = self.check_job_status(res.get(JOB_ID))

            # otherwise, wait until succeeds or fails
            tries = 0
            status = res.get(JOB_STATUS)
            while status in (JOB_EXECUTING, JOB_SUBMITTED):
                time.sleep(1)
                job = self.check_job_status(res.get(JOB_ID))
                status = job.get(JOB_STATUS)
                tries += 1
                if tries == timeout:
                    break

            if tries == timeout:
                # raise timeout error
                warnings.warn('GP Job Timed out after {} tries'.format(tries))
                return job

            if status == JOB_FAILED:
                # raise error for job failing
                raise RuntimeError('GP Job failed:\n{}'.format(json.dumps(job, indent=2)))

            elif status == JOB_SUCCEEDED:
                res = job.json
                res[JOB_URL] = '{}/{}/{}'.format(self.url, JOBS, res.get(JOB_ID))
        
        if ERROR in res:
            return GPTaskError(res)
                
        res['isAsync'] = self.isAsynchronous
        gp_elapsed = str(datetime.datetime.now() - start)
        res['elapsed'] = gp_elapsed
        print('GP Task "{}" completed successfully. (Elapsed time {})'.format(self.name, gp_elapsed))
        return GPTaskResponse(res)
