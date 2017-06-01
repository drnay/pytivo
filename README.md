# pyTivo

## Description

pyTivo lets you stream most videos from your PC to your unhacked tivo.

pyTivo is both an [HMO][HMO spec] and GoBack server. Similar to [TiVo Desktop][],
pyTivo loads many standard video compression codecs and outputs mpeg2 (or in some
cases, h.264) video to the TiVo. However, pyTivo is able to load many more file
types than TiVo Desktop.

pyTivo is in no way affiliated with [TiVo, Inc][TiVo]. 

The pyTivo information here and more is available on the [pyTivo Wiki][] hosted on Sourceforge.

## Requirements

OS = Anything that will run python and ffmpeg, which I think is
anything. Known to work on Linux, Mac OS X and Windows.

Python - http://www.python.org/download/

- You need python version >= 2.5 and < 3.0 i.e. get the latest version 2 Python.

pywin32 (only to install as a service) -
http://sourceforge.net/project/showfiles.php?group_id=78018&package_id=79063
- Windows users only and only if you intend to install as a service

## Usage

You need to edit pyTivo.conf in 3 places

1. ffmpeg=
2. [&lt;name of share>]
3. path=

`ffmpeg` ([download][ffmpeg download]) should be the full path to ffmpeg including filename.
`path` is the absolute path to your media, which may be a network share. See the comments
in the sample `pyTivo.conf.dist` file.

run `pyTivo.py`

### To install as a service in Windows

run `pyTivoService.py --startup auto install`

### To remove service

run `pyTivoService.py remove`

## Additional Help

1. [Frequently Asked Questions (FAQ)][pyTivo FAQ]
1. [pyTivo Forum][]
1. [pyTiVo thread][] at TiVo Community Forum 

## Notes
pyTivo was created by Jason Michalski ("armooo"). Contributors include
Kevin R. Keegan, William McBrine, and Terry Mound ("wgw").

[HMO spec]: <http://tivopod.sourceforge.net/tivohomemedia.pdf> "TiVo Home Media Option specification"
[TiVo Desktop]: <https://support.tivo.com/articles/Installation_Setup_Configuration/TiVo-Desktop-Desktop-Plus-for-PC-Installation-and-Use> "TiVo Desktop support"
[TiVo]: <https://www.tivo.com/> "TiVo website"
[pyTivo Wiki]: <https://pytivo.sourceforge.io/wiki/index.php/PyTivo> "pyTivo Wiki"
[ffmpeg download]: <https://ffmpeg.org/download.html> "Download FFmpeg"
[pyTivo FAQ]: <https://pytivo.sourceforge.io/wiki/index.php/Frequently_Asked_Questions> "pytivo FAQ"
[pyTivo Forum]: <https://pytivo.sourceforge.io/forum/> "pyTivo Forum"
[pyTiVo thread]: <http://www.tivocommunity.com/tivo-vb/showthread.php?t=328459> "pyTiVo thread on TiVo Community Forum"

## Setting up development on Ubuntu 16.04 linux

### python3

From DigitalOcean's [page on setting up Python 3](https://www.digitalocean.com/community/tutorials/how-to-install-python-3-and-set-up-a-programming-environment-on-an-ubuntu-16-04-server):

Install these apt packages:
- python3-pip
- python3-dev
- python3-venv
- pylint3
- build-essential
- libssl-dev
- libffi-dev

Create a Python 3 virtual environment for py3tivo (see DigitalOcean's page)

Install these python packages (with pip):
- zeroconf
- mutagen
- cheetah3

The required python packages are in `requirements.txt` and may be installed by running:

    pip install -r requirements.txt



