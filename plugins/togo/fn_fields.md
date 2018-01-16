# ToGo File Naming Fields #

The fields that may be used in the config file's [togo] section's *episode_fn* and *movie_fn* keys.

The values of these fields are retrieved from the TiVo. Because the TiVo no
longer supplies values for some of this metadata information (instead it
must be retrieved from the RPC mind server which pyTivo does not currently
support) some fields are not useful at this time.

Whether a file should be named as an episode or a movie is currently determined
by the existence of a value for the *movie_year* field, if it exists the
*movie_fn* key is used, otherwise the *episode_fn* key is used.

The syntax for the *episode_fn* and *movie_fn* keys is the Python [format string][pythonFormatStr].

Example settings:

```
episode_fn = {title} - s{season:d}e{episode:02d} - {episode_title} ({date_recorded:%b_%d_%Y},{callsign})
movie_fn = {title} ({movie_year}) ({date_recorded:%b_%d_%Y},{callsign})
```

## Fields ##

| Field nane       | Description |
| ---------------: | :---------- |
title              | The movie title or the series title. (string)
season             | **NOT POPULATED always 0** If a show, the season the episode occurs in. (integer)
episode            | **NOT POPULATED always 0** If a show, the episode within the season. (integer)
episode_title      | If a show, the title of the episode. (string)
date_recorded      | The start date/time of when the show was recorded. Default to now (datetime)
callsign           | The callsign of the channel the show was recorded from. (string)
channel            | The channel number the show was recorded from. (string)
movie_year         | If the recording is a movie the year it was released. (integer)
original_air_date  | The date/time the recording was originally aired. Default to Jan 1 1900 (datetime)
tivo_stream_type   | The method used to download the recording. (a string either "TS" or "PS")

Datetime fields have their own formatting specification.

[pythonFormatStr]: <>