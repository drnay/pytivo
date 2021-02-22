# Change Log

## [2.6.2] - 2021-02-22

update for python 3.9 and latest dependency package versions

### Fixed

- pylint removed bad-whitespace error. see [whatsnew 2.6](https://pylint.pycqa.org/en/latest/whatsnew/2.6.html)
- updating [zeroconf](https://github.com/jstasiak/python-zeroconf) (also see
  [latest docs](https://python-zeroconf.readthedocs.io/en/latest/api.html))
  from 0.24 to 0.28 required several changes in beacon.py
    - ServiceInfo address member was deprecated and then removed, addresses should now be used
    - ServiceInfo ctor argument order changed, and address was replaced by addresses
    - ServiceListeners will soon require an update_service method although it may do nothing
- python 3.9 plistlib module has removed the old API. It's used by metadata in a section of code
  that I do not think has ever been tested by me after updated from python 2 to 3. I've updated
  the code as I believe is needed, but it is *still* untested.
  see [python docs 3.5 plistlib](https://docs.python.org/3.5/library/plistlib.html)
- python 3.9 xml.xmlparser expat is giving errors (I'm not totally sure these errors didn't
  exist before). Some googling also made me believe in this instance the pylint errors may
  be false positives.
    - `metadata.py:608:23: E1101: Instance of 'module' has no 'codes' member (no-member)`
    - `metadata.py:608:42: E1101: Instance of 'module' has no 'XML_ERROR_INVALID_TOKEN' member (no-member)`
- fixed a typo error in plugins/video/video.py


## [2.6.1] - 2020-02-23

### Fixed

- Fix lrucache for PEP 479 implemented in python 3.6+. pyTivo now works with python 3.8


## [2.6.0] - 2018-06-10

### Changed

- Enhanced syncerr yaml report file by adding tivoName, attempt transfer info
  and error startMB.


## [2.5.1] - 2018-02-13

### Fixed

- fix crash getting NPL of a different TiVo than the one with active downloads.


## [2.5.0] - 2018-02-01

### Changed

- add new _ts_error_mode_ value **all** that saves all togo download attempts
- refactor the pyTivo version references in the code and add the version to
  the info page
- Update dependecies
    - mutagen (1.40)


## [2.4.0] - 2018-01-26

### Changed

- config fields for customized togo file naming; _episode_fn_ &  _movie_fn_,
  see [togo/fn_fields.md](./plugins/togo/fn_fields.md) for more info
- change logging priority of httpserver requests from info to debug to reduce
  noise when sending info priority messages to the console


## [2.3.0] - 2018-01-09

Some testing on Windows 7 in addition to Linux

### Changed

- Write a yaml sync error log file for every togo download
- Default to using transport stream downloads
- Update dependecies
    - mutagen (1.39)
    - pytz (2017.3)
    - tzlocal (1.5.1)
- Add a section to the Readme about installing on MS Windows

### Added

- Add some TiVo documentation so it's available in the future if needed.
- Add some development features like a Makefile to help running pylint and
  other development tasks


## [2.2.1] - 2017-07-30

### Fixed

- fix exception putting attempt number in filename
- improve logging


## [2.2.0] - 2017-07-29

### Changed

- Improve the logging during togo downloads
- Implement new ShowInfo class to encapsulate most of the show metadata
- Refactor download thread functionality
- Update dependecies, add tzlocal
    - mutagen (1.38)
    - zeroconf (0.19.1)
    - tzlocal (1.4)


## [2.1.0] - 2017-07-18

### Changed

- Incorporate Dan203's changes for his version PyTivo up through 1.6.7 except the
  pyInstaller and pyTivoTray which were more specifically for PyTivoDesktop and
  Windows & Mac not Linux.
    - Check transport stream (ts) downloads for sync errors
    - TivoDesktop togo file naming
    - support using tivolibre to decode the downloaded .tivo file
- moved togo settings to their own section in pytivo.conf ([togo])

### Fixed

- Implement thread safety for the togo download threads
- Log not finding a binary (e.g. tivodecode) only once per run

### Added

- new dependency
    - pytz (2017.2)


## [2.0.0] - 2017-06-20 (forked to https://github.com/mlippert/pytivo)

Tested only on Linux

### Changed

- Converted from Python 2 to **Python 3**
- Remove local copies of packages, use latest versions from pip
    - zeroconf (0.19.0)
    - mutagen (1.37)
    - cheetah3 (3.0.0)
- Format README with markdown
- Various info added to the README
- Prefix the episode title w/ the episode number when shown in a folder on the TiVo

### Added

- requirements.txt file for use w/ pip to install dependencies


## [1.6.0] - prior to 2017-01-01 (from https://github.com/wmcbrine/pytivo)

### Added

- All functionality of pyTivo up until this time. (I am not aware of any distinct
  versions or releases prior to this time. -mjl)
