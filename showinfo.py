import logging
from datetime import datetime
from collections import namedtuple
from collections.abc import Mapping
from functools import partial
from enum import Enum

import pytz


# I want to line up the values in dicts, lists and tuples such as
# the ShowInfo.FieldInfo default_val in metafields so:

# Various TV ratings strings associated with their rating value
TV_RATINGS = {1: ('Y7',    'TV-Y7', 'X1',    'TVY7'),
              2: ('Y',     'TV-Y',  'X2',    'TVY'),
              3: ('G',     'TV-G',  'X3',    'TVG'),
              4: ('PG',    'TV-PG', 'X4',    'TVPG'),
              5: ('14',    'TV-14', 'X5',    'TV14'),
              6: ('MA',    'TV-MA', 'X6',    'TVMA'),
              7: ('NR',    'TV-NR', 'X7',    'TVNR', 'UNRATED'),
             }

MPAA_RATINGS = {1: ('G',     'G1'),
                2: ('PG',    'P2'),
                3: ('PG-13', 'P3', 'PG13'),
                4: ('R',     'R4'),
                5: ('X',     'X5'),
                6: ('NC-17', 'N6', 'NC17'),
                8: ('NR',    'N8', 'UNRATED'),
               }

STAR_RATINGS = {1: ('ONE',              '1',   'X1', '*'),
                2: ('ONE_POINT_FIVE',   '1.5', 'X2'),
                3: ('TWO',              '2',   'X3', '**'),
                4: ('TWO_POINT_FIVE',   '2.5', 'X4'),
                5: ('THREE',            '3',   'X5', '***'),
                6: ('THREE_POINT_FIVE', '3.5', 'X6'),
                7: ('FOUR',             '4',   'X7', '****'),
               }

TV_ADVISORIES = {2:  ('GRAPHIC_LANGUAGE',),
                 6:  ('VIOLENCE',),
                 10: ('ADULT_SITUATIONS',),
                }

ICON_URN_TO_NAME = {'urn:tivo:image:expires-soon-recording':        'expiring',
                    'urn:tivo:image:expired-recording':             'expired',
                    'urn:tivo:image:save-until-i-delete-recording': 'kuid',
                    'urn:tivo:image:suggestion-recording':          'suggestion',
                    'urn:tivo:image:in-progress-recording':         'inprogress',
                   }

# Constant strings in the description from the tivo we don't want to preserve (takes up too much space)
TRIBUNE_CR = ' Copyright Tribune Media Services, Inc.'
ROVI_CR = ' Copyright Rovi, Inc.'

# Time constants
MS_PER_SEC = 1000
MS_PER_MIN = 60 * MS_PER_SEC
MS_PER_HOUR = 60 * MS_PER_MIN
MS_PER_DAY = 24 * MS_PER_HOUR

class ShowTypeValues(Enum):
    """
    Values sent by the TiVo in the showType field.

    I've gleaned these values from examining the xml from shows I've recorded.
    I'm sure there's a definitive list somewhere but I haven't found it. -mjl
    """
    SERIES = 5
    MOVIE = 8

class DataSources(Enum):
    """
    Enumeration of the possible sources of ShowInfo metadata.
    """
    TIVO_CONTAINER_ITEM = 'tivo-container-item'
    TIVO_ITEM_DETAILS = 'tivo-item-details'

class ShowInfo(Mapping):
    """
    Encapsulate all information (metadata) about a show (recording) on a TiVo.

    see https://pytivo.sourceforge.io/forum/metagenerator-version-3-t1786-825.html
    """
    # All of the metadata fields that are provided by the TiVo (that we care about)
    NamedValue = namedtuple('NamedValue', ['value', 'name'])
    FieldInfo = namedtuple('FieldInfo', ['name', 'default_val'])
    metafields = (FieldInfo('title',            ''),
                  FieldInfo('series_title',     ''),
                  FieldInfo('episode_title',    ''),
                  FieldInfo('combined_ep_no',   0),                     # ep number provide by TiVo usually can be split to season and episode
                  FieldInfo('season_number',    0),                     # cmb_ep/100 = season
                  FieldInfo('episode_number',   0),                     # cmb_ep%100 = episode

                  # part count and index may be included if it is a multipart episode. If applicable,
                  # both must be included. Ex. part_count : 2, part_index : 1. Will appear on the tivo
                  # info screen as "Part Index 1 of 2"
                  FieldInfo('part_count',       0),
                  FieldInfo('part_index',       0),

                  FieldInfo('movie_year',       0),                     # ex 1971
                  FieldInfo('description',      ''),
                  FieldInfo('capture_date',     None),                  # datetime instance
                  FieldInfo('original_air_date',None),                  # datetime instance
                  FieldInfo('duration',         0),                     # milliseconds
                  FieldInfo('source_size',      0),                     # bytes
                  FieldInfo('station_callsign', ''),                    # ex 'WABC' or 'SYFYHD'
                  FieldInfo('station_channel',  ''),                    # major[-minor] ex '641' or '18-2'

                  # From philhu: https://pytivo.sourceforge.io/forum/post16931.html#16931
                  # isEpisodic is supposed to be used when a show episodes do not have
                  # descriptions and a generic description is used for all episodes. This
                  # flag says whether the show expects to be listed as episodes or just
                  # programs.
                  # It is kinda overwritten by the episode meta 'isEpisode' which
                  # specifies whether the program being described is an episode or a
                  # special for the series. Like a blooper reel or a season special, etc.
                  FieldInfo('is_episode',       False),
                  FieldInfo('is_episodic',      False),

                  FieldInfo('tv_rating',        NamedValue(None, '')),  # tv rating value and name given (see TV_RATINGS)
                  FieldInfo('mpaa_rating',      NamedValue(None, '')),  # mpaa rating value and name given (see MPAA_RATINGS)
                  FieldInfo('star_rating',      NamedValue(None, '')),  # star rating value and name given (see STAR_RATINGS)
                  FieldInfo('show_type',        NamedValue(None, '')),  # show type value and name, the 2 I know of are: (5, 'SERIES') and (8, 'MOVIE')`
                  FieldInfo('series_id',        ''),                    # ex 'SH0345054743' empirically series start w/ 'SH' and movies with 'MV'
                  FieldInfo('program_id',       ''),                    # ex 'EP0345054743-0355598828'
                  FieldInfo('showing_bits',     ''),                    # ex '135169'
                  FieldInfo('actors',           []),                    # list of name strings: '<last>|<first>'
                  FieldInfo('guest_stars',      []),                    #  "
                  FieldInfo('directors',        []),                    #  "
                  FieldInfo('exec_producers',   []),                    #  "
                  FieldInfo('producers',        []),                    #  "
                  FieldInfo('writers',          []),                    #  "
                  FieldInfo('choreographers',   []),                    #  "
                  FieldInfo('hosts',            []),                    #  "
                  FieldInfo('advisories',       []),                    # list of advisory NamedValues ex (6, 'VIOLENCE')
                  FieldInfo('program_genres',   []),                    # list of genre strings (I'm not sure where this will come from but kmttg has it)

                  # These fields contain information about the show currently on a particular TiVo
                  FieldInfo('download_url',     ''),                    # URL that will download this show from the TiVo
                  FieldInfo('details_url',      ''),                    # URL that will retrieve an xml doc w/ more detailed metadata from the TiVo
                  FieldInfo('in_progress',      False),                 # If the show is in progress of being recorded on the TiVo
                  FieldInfo('is_protected',     False),                 # If the show is copy protected and cannot be downloaded from the TiVo
                  FieldInfo('is_suggestion',    False),                 # If the show is a TiVo suggestion vs having been specifically requested to be recorded
                  FieldInfo('icon',             'normal'),              # The icon to use to represent this show's state in a list of shows
                 )

    def __init__(self):
        self.logger = logging.getLogger('pyTivo.ShowInfo')

        self.show_metadata = {fi.name: fi.default_val for fi in ShowInfo.metafields}

        # Keep track of where this ShowInfo got its metadata values (see DataSources enum)
        self.data_sources = set()

    def __contains__(self, item):
        """Override for abc Container which is the base of Mapping"""
        return item in self.show_metadata

    def __len__(self):
        """Override for abc Mapping"""
        return len(self.show_metadata)

    def __getitem__(self, key):
        """Override for abc Mapping"""
        return self.show_metadata[key]

    def __iter__(self):
        """Override for abc Mapping"""
        return self.show_metadata.keys()


    def is_movie(self):
        """
        From the known information, make the best guess as to if this show is a movie
        """
        # Consider the show_type the definitive answer
        if self.show_metadata['show_type'].value == ShowTypeValues.MOVIE:
            return True

        # Other indications not quite as good:
        return (self.show_metadata['movie_year'] > 0 or
                self.show_metadata['series_id'].startswith('MV') or
                not self.show_metadata['episode_title'])


    def from_tivo_container_item(self, item):
        """
        Update this ShowInfo from the item xml element tree sent by a TiVo for
        items in a container (folder).
        """
        if DataSources.TIVO_CONTAINER_ITEM in self.data_sources:
            self.logger.warning('Values from a tivo container item have already been read for this ShowInfo')
        else:
            self.data_sources.add(DataSources.TIVO_CONTAINER_ITEM)

        Retrieve = namedtuple('Retrieve', ['field', 'xpath', 'process'])
        item_fields = (Retrieve('title',            'Details/Title',              _identity),
                       Retrieve('episode_title',    'Details/EpisodeTitle',       _identity),
                       Retrieve('combined_ep_no',   'Details/EpisodeNumber',      int),         # Note: this is rarely if ever there anymore 7/20/2017
                       Retrieve('description',      'Details/Description',        _clean_description),
                       Retrieve('capture_date',     'Details/CaptureDate',        _xtime2datetime),
                       Retrieve('duration',         'Details/Duration',           int),
                       Retrieve('source_size',      'Details/SourceSize',         int),
                       Retrieve('station_callsign', 'Details/SourceStation',      _identity),
                       Retrieve('station_channel',  'Details/SourceChannel',      _identity),
                       Retrieve('tv_rating',        'Details/TvRating',           _tvrating_v2nmval),
                       Retrieve('mpaa_rating',      'Details/MpaaRating',         _mpaarating_v2nmval),
                       Retrieve('series_id',        'Details/SeriesId',           _identity),
                       Retrieve('program_id',       'Details/ProgramId',          _identity),
                       Retrieve('showing_bits',     'Details/ShowingBits',        _identity),

                       Retrieve('download_url',     'Links/Content/Url',          _identity),
                       Retrieve('details_url',      'Links/TiVoVideoDetails/Url', _identity),
                       Retrieve('in_progress',      'Details/InProgress',         _str2bool),
                       Retrieve('is_protected',     'Details/CopyProtected',      _str2bool),
                       Retrieve('is_suggestion',    'Links/CustomIcon/Url',       _is_suggestion_icon),
                       Retrieve('icon',             'Links/CustomIcon/Url',       _custom_icon),
                      )

        # The details child element contains a lot of values, so we optimize by getting it once
        item_details = item.getElementsByTagName('Details')[0]
        def try_use_details(xpath):
            if xpath.startswith('Details/'):
                return (item_details, xpath[8:])
            return (item, xpath)

        # update all metadata fields that have information in the given item xml element tree
        for f in item_fields:
            try:
                raw_val = Xml_utils.get_path_text(*try_use_details(f.xpath))
                if raw_val:
                    self.show_metadata[f.field] = f.process(raw_val)
            except Exception as e:              # pylint: disable=broad-except
                self.logger.info('Unable to process "%s" field from container item', f.field)
                self.logger.debug('from_tivo_container_item: raised %s: %s\n\t%s (%r)', e.__class__.__name__, e, f, raw_val)

        # override the icon (custom or not) if the show is copy protected
        if self.show_metadata['is_protected']:
            self.show_metadata['icon'] = 'protected'


    def from_tivo_details(self, details):
        """
        Update this ShowInfo from the details xml element tree sent by a TiVo for
        a particular recording.

        Note: I've dropped getting the vProgramGenre list because it seems to always
        consist of 4 empty elements. -mjl
        """
        if DataSources.TIVO_ITEM_DETAILS in self.data_sources:
            self.logger.warning('Values from a tivo\'s item details have already been read for this ShowInfo')
        else:
            self.data_sources.add(DataSources.TIVO_ITEM_DETAILS)

        txt = Xml_utils.get_path_text
        nmval = Xml_utils.get_path_namedvalue
        l_txt = partial(Xml_utils.get_path_text_list, list_element_name='element')
        l_nmval = partial(Xml_utils.get_path_namedvalue_list, list_element_name='element')

        # fields available from the tivo details (commented out fields are
        # available but should not override the values from the tivo_container_item
        # and therefore should not be retrieved)
        Retrieve = namedtuple('Retrieve', ['field', 'xpath', 'get', 'process'])
        detail_fields = (Retrieve('title',             'showing/title',                      txt,   _identity),
                         Retrieve('series_title',      'showing/program/series/seriesTitle', txt,   _identity),
                         Retrieve('episode_title',     'showing/program/episodeTitle',       txt,   _identity),
                         Retrieve('combined_ep_no',    'showing/program/episodeNumber',      txt,   int),               # Note: this is rarely if ever there anymore 7/20/2017
                         Retrieve('part_count',        'showing/partCount',                  txt,   int),
                         Retrieve('part_index',        'showing/partIndex',                  txt,   int),
                         Retrieve('movie_year',        'showing/program/movieYear',          txt,   int),               # <movieYear>2008</movieYear>
                         Retrieve('description',       'showing/program/description',        txt,   _clean_description),
                         Retrieve('capture_date',      'showing/time',                       txt,   _dtstr2datetime),   # <time>2017-07-14T10:00:00Z</time>
                         Retrieve('original_air_date', 'showing/program/originalAirDate',    txt,   _dtstr2datetime),   # <originalAirDate>2017-02-10T00:00:00Z</originalAirDate>
                         #Retrieve('duration',          'recordedDuration',                   txt,   _iso2ms),           # <recordedDuration>PT59M57S</recordedDuration>
                         Retrieve('is_episode',        'showing/program/isEpisode',          txt,   _str2bool),         # <isEpisode>true</isEpisode>
                         Retrieve('is_episodic',       'showing/program/series/isEpisodic',  txt,   _str2bool),         # <isEpisodic>false</isEpisodic>
                         Retrieve('tv_rating',         'showing/tvRating',                   nmval, _identity),         # <tvRating value="5">_14</tvRating>
                         Retrieve('mpaa_rating',       'showing/program/mpaaRating',         nmval, _identity),         # <mpaaRating value="3">PG_13</mpaaRating>
                         Retrieve('star_rating',       'showing/program/starRating',         nmval, _identity),         # <starRating value="4">TWO_POINT_FIVE</starRating>
                         Retrieve('show_type',         'showing/program/showType',           nmval, _identity),         # <showType value="8">MOVIE</showType>
                         #Retrieve('series_id',         'showing/program/series/uniqueId',    txt    _identity),         # Note: I don't see this anymore 7/20/2017
                         #Retrieve('program_id',        'showing/program/uniqueId',           txt,   _identity),         # Note: I don't see this anymore 7/20/2017
                         #Retrieve('showing_bits',      'showing/showingBits',                txt,   _identity),         # <showingBits value="4099"/>
                         Retrieve('actors',            'showing/program/vActor',             l_txt,   _identity),
                         Retrieve('guest_stars',       'showing/program/vGuestStar',         l_txt,   _identity),
                         Retrieve('directors',         'showing/program/vDirector',          l_txt,   _identity),
                         Retrieve('exec_producers',    'showing/program/vExecProducer',      l_txt,   _identity),
                         Retrieve('producers',         'showing/program/vProducer',          l_txt,   _identity),
                         Retrieve('writers',           'showing/program/vWriter',            l_txt,   _identity),
                         Retrieve('advisories',        'showing/program/vAdvisory',          l_nmval, _identity),
                         Retrieve('choreographers',    'showing/program/vChoreographer',     l_txt,   _identity),
                         Retrieve('hosts',             'showing/program/vHost',              l_txt,   _identity),
                        )

        # The optimize getting child elements by starting the path traversal at
        # a closer ancestor.
        showing = details.getElementsByTagName('showing')[0]
        program = showing.getElementsByTagName('program')[0]
        parent_path = (('showing/program/', program), ('showing/', showing)) # order matters
        def add_parent(xpath):
            for prefix, parent in parent_path:
                if xpath.startswith(prefix):
                    return (parent, xpath[len(prefix):])
            return (details, xpath)

        # update all metadata fields that have information in the given item xml element tree
        for f in detail_fields:
            try:
                raw_val = f.get(*add_parent(f.xpath))
                if raw_val:
                    self.show_metadata[f.field] = f.process(raw_val)
            except Exception as e:              # pylint: disable=broad-except
                self.logger.info('Unable to process "%s" field from details', f.field)
                self.logger.debug('from_tivo_details: raised %s: %s\n\t%s (%r)', e.__class__.__name__, e, f, raw_val)

    def get_pytivo_desktop_info(self):
        """
        Get the showinfo in the format desired by pyTivo Desktop
        """
        ep_info = {'seriesID':      self['series_id'],
                   'episodeID':     self['program_id'],
                   'url':           self['download_url'],
                   'title':         self['title'],
                   'detailsUrl':    self['details_url'],
                   'episodeTitle':  self['episode_title'],
                   'description':   self['description'],
                   'recordDate':    self['capture_date'],
                   'duration':      self['duration'],
                   'sourceSize':    self['source_size'],
                   'channel':       self['station_channel'],
                   'stationID':     self['station_callsign'],
                   'inProgress':    self['in_progress'],
                   'isProtected':   self['is_protected'],
                   'isSuggestion':  self['is_suggestion'],
                   'icon':          self['icon'],
                  }

        return ep_info

    def get_old_basicmeta(self):
        """
        Get the metadata in the format provided by the old metadata.from_container call
        """
        container_info = {'title':              self['title'],
                          'episodeTitle':       self['episode_title'],
                          'description':        self['description'],
                          'programId':          self['program_id'],
                          'seriesId':           self['series_id'],
                          'episodeNumber':      self['combined_ep_no'],
                          'tvRating':           self['tv_rating'].value,
                          'displayMajorNumber': self['station_channel'],
                          'callsign':           self['station_callsign'],
                          'showingBits':        self['showing_bits'],
                          'mpaaRating':         self['mpaa_rating'].name,
                          'recordDate':         self['capture_date'],
                         }

        if (container_info['displayMajorNumber'] and
                '-' in container_info['displayMajorNumber']):
            major, minor = container_info['displayMajorNumber'].split('-')
            container_info['displayMajorNumber'] = major
            container_info['displayMinorNumber'] = minor

        # return w/o any key whose value is falsy
        return {k: container_info[k] for k in container_info if container_info[k]}


    def write_text(self, f_out):
        """
        Write the known metadata to a metadata text file
        """
        # A sequence lets us preserve the order we want to write the fields out.
        TextField = namedtuple('TextField', ['out_name', 'show_name', 'format'])
        text_fields = (TextField('title',              'title',              _identity),
                       TextField('seriesTitle',        'series_title',       _identity),
                       TextField('episodeTitle',       'episode_title',      _identity),
                       TextField('description',        'description',        _identity),
                       TextField('episodeNumber',      'combined_ep_no',     _identity),
                       TextField('movieYear',          'movie_year',         _identity),
                       TextField('time',               'capture_date',       _v_datetime),
                       TextField('originalAirDate',    'original_air_date',  _v_datetime),
                       TextField('iso_duration',       'duration',           _v_isoduration),
                       TextField('callsign',           'station_callsign',   _identity),
                       TextField('displayMajorNumber', 'station_channel',    _v_major_no),
                       TextField('displayMinorNumber', 'station_channel',    _v_minor_no),
                       TextField('isEpisode',          'is_episode',         _identity),
                       TextField('isEpisodic',         'is_episodic',        _identity),
                       TextField('tvRating',           'tv_rating',          _v_tvrating),
                       TextField('mpaaRating',         'mpaa_rating',        _v_mpaarating),
                       TextField('seriesId',           'series_id',          _identity),
                       TextField('programId',          'program_id',         _identity),
                       TextField('showingBits',        'showing_bits',       _identity),
                       TextField('vActor',             'actors',             _identity),
                       TextField('vGuestStar',         'guest_stars',        _identity),
                       TextField('vDirector',          'directors',          _identity),
                       TextField('vExecProducer',      'exec_producers',     _identity),
                       TextField('vProducer',          'producers',          _identity),
                       TextField('vWriter',            'writers',            _identity),
                       TextField('vChoreographer',     'choreographers',     _identity),
                       TextField('vHost',              'hosts',              _identity),
                       TextField('vAdvisory',          'advisories',         _v_nv_name),
                       TextField('vProgramGenre',      'program_genres',     _identity),
                      )

        def write_field(k, v, fmt):
            try:
                fmt_v = fmt(v)
                if fmt_v:
                    f_out.write('{}: {}\n'.format(k, fmt_v))
            except Exception as e:              # pylint: disable=broad-except
                self.logger.info('Unable to write "%s" field', k)
                self.logger.debug('write_field: raised %s: %s\n\t(%s, %s, %s)', e.__class__.__name__, e, k, v, fmt)

        for field in text_fields:
            k, v = field.out_name, self[field.show_name]
            if not v:
                continue
            if isinstance(v, list):
                for element in v:
                    write_field(k, element, field.format)
            else:
                write_field(k, v, field.format)


# Post process functions used to convert source metadata to the format we want it in
def _clean_description(d):
    d = d.replace(TRIBUNE_CR, '').replace(ROVI_CR, '')
    if d.endswith(' *'):
        d = d[:-2]
    return d

_identity = lambda x: x
_xtime2datetime = lambda t: datetime.utcfromtimestamp(int(t, 16))
_dtstr2datetime = lambda dtstr: datetime.strptime(dtstr, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=pytz.utc) # ex. 2017-07-14T10:00:00Z
_str2bool = lambda x: x.lower() in ('true', 'yes', 'on', '1')
_is_suggestion_icon = lambda urn: urn == 'urn:tivo:image:suggestion-recording'
_custom_icon = lambda urn: ICON_URN_TO_NAME[urn] if urn in ICON_URN_TO_NAME else 'normal'
_tvrating_v2nmval = lambda v: ShowInfo.NamedValue(int(v), TV_RATINGS[int(v)][0])
_mpaarating_v2nmval = lambda v: ShowInfo.NamedValue(int(v), MPAA_RATINGS[int(v)][0])

# Post process functions to convert our data to a destination format
def _v_isoduration(ms):
    days = ms // MS_PER_DAY
    ms -= days * MS_PER_DAY
    hours = ms // MS_PER_HOUR
    ms -= hours * MS_PER_HOUR
    minutes = ms // MS_PER_MIN
    ms -= minutes * MS_PER_MIN
    seconds = ms // MS_PER_SEC
    iso8601 = ['P']
    iso8601 += (str(days), 'D') if days else ()
    iso8601 += ['T']
    iso8601 += (str(hours), 'H') if hours else ()
    iso8601 += (str(minutes), 'M') if minutes else ()
    iso8601 += (str(seconds), 'S') if seconds else ()
    return ''.join(iso8601)

_v_nv_name = lambda nv: nv.name
_v_datetime = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%SZ') if dt else ''
_v_tvrating = lambda nv: TV_RATINGS[nv.value][0] if nv.value else ''        # kmttg uses [2] in their metadata txt file
_v_mpaarating = lambda nv: MPAA_RATINGS[nv.value][0] if nv.value else ''    # kmttg uses [1] in their metadata txt file
_v_major_no = lambda ch: ch if '-' not in ch else ch.split('-')[0]
_v_minor_no = lambda ch: ch.split('-')[1] if '-' in ch else None


class Xml_utils():
    """
    Helper functions to make getting values from an xml document easier.
    """

    @staticmethod
    def get_child(parent, path):
        """
        Get the parent's child element by following the simple path
        (path only lets you navigate to the 1st child of a given name)
        """
        element = parent
        for name in path.split('/'):
            found = False
            for new_element in element.childNodes:
                if new_element.nodeName == name:
                    found = True
                    element = new_element
                    break
        return element if found else None


    @staticmethod
    def get_text(element):
        """
        Get the value of the text node child of the given element,
        returns an empty string if no child text node is found.
        """
        if element is None:
            return ''

        text_node = element.firstChild
        if not (text_node and text_node.nodeType == text_node.TEXT_NODE):
            return ''

        return text_node.data


    @staticmethod
    def get_attr_value(element, attrname):
        """
        Get the value of the attribute of the given element,
        returns None if no attribute is found.
        """
        if element is None or attrname not in element.attributes:
            return None

        return element.attributes[attrname].value


    @staticmethod
    def get_path_text(parent, path):
        """
        Get the text from the child element found by traversing
        the parent tree using the given path.
        """
        return Xml_utils.get_text(Xml_utils.get_child(parent, path))


    @staticmethod
    def get_namedvalue(element):
        """
        Get a NamedValue where the value is the value of the 'value'
        attribute of the element and the name the text of the element.
        """
        if element is None:
            return None

        n = Xml_utils.get_text(element)
        v = Xml_utils.get_attr_value(element, 'value')
        if v is None:
            return None

        return ShowInfo.NamedValue(value=int(v), name=n)


    @staticmethod
    def get_path_namedvalue(parent, path):
        """
        Get a NamedValue where the value is the value of the 'value'
        attribute of the found child element and the name is that
        element's text.
        """
        return Xml_utils.get_namedvalue(Xml_utils.get_child(parent, path))


    @staticmethod
    def get_path_text_list(parent, path, list_element_name):
        """
        Get a list consisting of the text of all list_element_name child
        elements of the path element.
        """
        list_container = Xml_utils.get_child(parent, path)
        if list_container is None:
            return []

        return [Xml_utils.get_text(e) for e in list_container.childNodes if e.nodeName == list_element_name]


    @staticmethod
    def get_path_namedvalue_list(parent, path, list_element_name):
        """
        Get a list consisting of the namedvalue of all list_element_name child
        elements of the path element.
        """
        list_container = Xml_utils.get_child(parent, path)
        if list_container is None:
            return []

        return [Xml_utils.get_namedvalue(e) for e in list_container.childNodes if e.nodeName == list_element_name]
