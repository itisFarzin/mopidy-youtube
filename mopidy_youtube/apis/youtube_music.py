import json
import re
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor

from ytmusicapi import YTMusic

from mopidy_youtube import logger
from mopidy_youtube.apis import youtube_japi
from mopidy_youtube.comms import Client
from mopidy_youtube.youtube import Video

ytmusic = None
own_channel_id = None


# Direct access to YouTube Music API
#
class Music(Client):
    endpoint = None

    def __init__(self, proxy, headers, *args, **kwargs):
        global ytmusic
        super().__init__(proxy, headers, *args, **kwargs)
        auth = (
            None
            if headers.get("Cookie") == "PREF=hl=en; CONSENT=YES+20210329;"
            else json.dumps(headers)
        )
        try:
            ytmusic = YTMusic(auth=auth)
        except Exception as e:
            logger.error("YTMusic init error: %s", str(e))
            ytmusic = YTMusic()

    @classmethod
    def search(cls, q):
        result = []
        search_functions = [cls.search_albums, cls.search_songs]

        with ThreadPoolExecutor() as executor:
            futures = []
            for search_function in search_functions:
                futures.append(executor.submit(search_function, q))
            for future in as_completed(futures):
                result.extend(future.result()[: int(Video.search_results)])

        return json.loads(json.dumps({"items": result}))

    @classmethod
    def list_related_videos(cls, video_id):
        """
        returns related videos for a given video_id
        """
        tracks = []
        related_videos = ytmusic.get_watch_playlist(video_id)
        tracks = [
            cls.ytplaylist_item_to_video(track)
            for track in related_videos["tracks"]
        ]

        # sometimes, ytmusic.get_watch_playlist seems to return very few, or even
        # only one, related video, which may be the original video, itself.  If this
        # happens, get related videos using the bs4API.

        if len(tracks) < 10:
            japi_related_videos = youtube_japi.jAPI.list_related_videos(
                video_id
            )
            japi_related_videos["items"].extend(tracks)
            return japi_related_videos

        return json.loads(
            json.dumps({"items": tracks}, sort_keys=False, indent=1)
        )

    @classmethod
    def list_channelplaylists(cls, channel_id):

        # this really should be ytmusic.get_user_playlists(), I think, with channel_id
        # controlling which channel's (user's) playlists are retrieved. get_library_playlists()
        # allows only the playlists of the authenticated user.
        # sigma67 says that ytmusic.get_user_playlists should work without authentication
        # but I can't get it to work.

        results = []
        channelTitle = None
        # if channel_id is None or own_channel_id then try to retrieve public and private playlists
        if channel_id in (None, own_channel_id):
            try:
                logger.debug(
                    "ytmusic.get_library_playlists triggered: youtube-music list_channelplaylists"
                )
                results = ytmusic.get_library_playlists()
                if channel_id:
                    logger.debug(
                        "ytmusic.get_user triggered: youtube-music list_channelplaylists"
                    )
                    channelTitle = ytmusic.get_user(channel_id)["name"]
                else:
                    channelTitle = "unknown"

            except Exception as e:
                logger.debug(f"list_channelplaylists exception {e}")
                if channel_id:
                    logger.debug(
                        "ytmusic.get_user triggered: youtube-music list_channelplaylists"
                    )
                    user = ytmusic.get_user(channel_id)
                    results = user["playlists"]["results"]
                    channelTitle = user["name"]

        else:  # if channel_id is not None and not own_channel_id retrieve only public playlists:
            logger.debug(
                "ytmusic.get_user triggered: youtube-music list_channelplaylists"
            )
            user = ytmusic.get_user(channel_id)
            results = user["playlists"]["results"]
            channelTitle = user["name"]

        items = [
            {
                "id": item["playlistId"],
                "contentDetails": {
                    "itemCount": int(item.get("count", "1").replace(",", ""))
                },
                "snippet": {
                    "title": item.get("title", "Unknown"),
                    "resourceId": {"playlistId": item["playlistId"]},
                    # TODO: full support for thumbnails
                    "thumbnails": {"default": item["thumbnails"][0]},
                    "channelTitle": channelTitle,
                },
            }
            for item in results
        ]
        return json.loads(
            json.dumps({"items": items}, sort_keys=False, indent=1)
        )

    @classmethod
    def search_songs(cls, q):
        logger.debug("ytmusic.search triggered: youtube-music search_songs")
        results = ytmusic.search(
            query=q, filter="songs", limit=Video.search_results
        )

        songs = [
            {
                "id": {"kind": "youtube#video", "videoId": item["videoId"],},
                "contentDetails": {
                    "duration": "PT"
                    + cls.format_duration(
                        re.match(cls.time_regex, item["duration"])
                    )
                },
                "snippet": {
                    "title": item["title"],
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": item["videoId"],
                    },
                    # TODO: full support for thumbnails
                    "thumbnails": {"default": item["thumbnails"][0]},
                    "channelTitle": item["artists"][0]["name"],
                    "album": item["album"],
                    "artists": item["artists"],
                },
            }
            for item in results
        ]
        return songs

    @classmethod
    def ytplaylist_item_to_video(cls, item, thumbnail=None):
        video = {}
        if "duration" in item:
            duration = item["duration"]
        else:
            duration = item["length"]

        if "artists" in item:
            channelTitle = item["artists"][0]["name"]
        else:
            channelTitle = item["byline"]

        if thumbnail is None and "thumbnail" in item:
            thumbnail = item["thumbnail"][-1]

        video.update(
            {
                "id": {"kind": "youtube#video", "videoId": item["videoId"],},
                "contentDetails": {
                    "duration": "PT"
                    + cls.format_duration(re.match(cls.time_regex, duration))
                },
                "snippet": {
                    "title": item["title"],
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": item["videoId"],
                    },
                    # TODO: full support for thumbnails
                    "thumbnails": {"default": thumbnail},
                    "channelTitle": channelTitle,
                },
            }
        )
        return video

    @classmethod
    def ytalbum_item_to_video(cls, item, thumbnail):
        def _convertMillis(milliseconds):
            try:
                hours, miliseconds = divmod(int(milliseconds), 3600000)
            except Exception:
                return "00:00:00"
            minutes, miliseconds = divmod(miliseconds, 60000)
            seconds = int(miliseconds) / 1000
            return "%i:%02i:%02i" % (hours, minutes, seconds)

        if "duration" in item:
            duration = item["duration"]
        elif "length" in item:
            duration = item["length"]
        elif "lengthMs" in item:
            duration = _convertMillis(item["lengthMs"])
        else:
            duration = "0:00"
            logger.info(f"duration missing: {item}")

        video = {}
        video.update(
            {
                "id": {"kind": "youtube#video", "videoId": item["videoId"],},
                "contentDetails": {
                    "duration": "PT"
                    + cls.format_duration(re.match(cls.time_regex, duration))
                },
                "snippet": {
                    "title": item["title"],
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": item["videoId"],
                    },
                    # TODO: full support for thumbnails
                    "thumbnails": {"default": thumbnail},
                    "channelTitle": item["artists"],
                },
            }
        )
        return video

    @classmethod
    def search_albums(cls, q):
        albums = []
        logger.debug("ytmusic.search triggered: youtube-music search_albums")
        results = ytmusic.search(
            query=q, filter="albums", limit=Video.search_results
        )

        def job(result):
            try:
                logger.debug(
                    f"ytmusic.get_album({result['browseId']}) triggered: youtube-music search_albums"
                )
                ytmusic_album = ytmusic.get_album(result["browseId"])
                album = {
                    "id": {
                        "kind": "youtube#playlist",
                        "playlistId": result["browseId"],
                    },
                    "snippet": {
                        "channelTitle": result["type"],
                        "thumbnails": {"default": result["thumbnails"][0]},
                        "title": result["title"],
                    },
                    "contentDetails": {
                        "itemCount": ytmusic_album["trackCount"]
                    },
                    "tracks": ytmusic_album["tracks"],
                }

                return album

            except Exception as e:
                logger.error('youtube_music search_albums error "%s"', e)

        with ThreadPoolExecutor() as executor:
            futures = executor.map(job, results)
            [albums.append(value) for value in futures]

        return albums

    @classmethod
    def list_playlists(cls, ids):
        """
        list playlists
        """

        # what follows works, but it loads each playlist separately.
        # So, if you have 50 playlists that's 50 trips to the endpoint.
        results = []

        def job(id):
            try:
                logger.debug(
                    "ytmusic.get_album triggered: youtube-music list_playlists"
                )
                result = ytmusic.get_album(browseId=id)
                return result
            except Exception as e:
                logger.debug(
                    f"ytmusic.get_album failed with {e} for playlist {id}"
                )

        with ThreadPoolExecutor() as executor:
            futures = executor.map(job, ids)
            [results.append(value) for value in futures if value is not None]

        if len(results) == 0:
            # why would this happen?
            logger.debug(f"list_playlists for {ids} returned no results")
            return None

        items = [
            {
                "id": result["playlistId"],
                "snippet": {
                    "title": result["title"],
                    "thumbnails": {"default": result["thumbnails"][0]},
                    # apparently, result["artist"] can be empty??
                    "channelTitle": result["artist"][0]["name"],
                },
                "contentDetails": {"itemCount": result["trackCount"]},
            }
            for result in results
        ]

        # get the videos in the playlist and
        # start loading video info in the background
        # this isn't really part of the api - should it be removed? does it
        # speed anything up?

        # tracks = [cls.ytalbum_item_to_video(track, result["thumbnails"][0]) for result in results for track in result["tracks"]]
        # Music.loadTracks(tracks)

        return json.loads(
            json.dumps({"items": items}, sort_keys=False, indent=1)
        )

    @classmethod
    def loadTracks(cls, tracks):

        videos = [Video.create_object(track) for track in tracks]

        Video.load_info(videos)

    @classmethod
    def list_playlistitems(cls, id, page=None, max_results=None):
        if id.startswith("PL"):
            logger.debug(
                f"ytmusic.get_playlist({id}) triggered: youtube-music list_playlistitems"
            )
            result = ytmusic.get_playlist(id)
            items = [
                cls.ytplaylist_item_to_video(item, item["thumbnails"][0])
                for item in result["tracks"]
            ]
        else:
            logger.debug(
                f"ytmusic.get_album({id}) triggered: youtube-music list_playlistitems"
            )
            result = ytmusic.get_album(id)

            items = [
                cls.ytalbum_item_to_video(item, result["thumbnails"][0])
                for item in result["tracks"]
            ]

        # why do ytplaylist_item_to_video and ytalbum_item_to_video both include
        # {"id": {"kind": "youtube#video", "videoId": item["videoId"],}} instead of
        # {"id": item["videoId"]}?

        # And, given that they do include the longer one, why isn't the following
        # necessary for compatibility with the youtube API?

        [
            item.update({"id": item["id"]["videoId"]})
            for item in items
            if "videoId" in item["id"]
        ]

        # Because Playlist.videos gets the id from {"snippet": {"resourceId":
        # {"videoId": item["videoId"]},}}. But it doesn't hurt to keep them consistent.

        ajax = None
        return json.loads(
            json.dumps(
                {"nextPageToken": ajax, "items": items},
                sort_keys=False,
                indent=1,
            )
        )
