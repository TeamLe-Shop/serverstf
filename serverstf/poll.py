"""Server state poller.

This module implements the ``poll`` subcommand which is a simple service
that takes responsibility for keeping the server state cache up to date.

It watches a Redis list for :class:`serverstf.cache.Address`es to poll.
When a server is polled A2S requests are issued to it and the tags are
re-evaluated. This updated state is then comitted to the cache.

The poller has two modes: normal and passive. In the normal mode the poller
watches the so-called 'interest queue'. The interest queue is a kind of
priority queue where the ammount of *interest* in an address determines how
many occurrences of it there is the queue and hence controls how frequently
it gets polled.

In passive mode the poller simply polls all servers known to the cache.
This is done in an attempt to prevent cache states becoming too stale if
they're not in the interest queue.
"""

import asyncio
import datetime
import logging

import geoip2.database
import maxminddb
import valve.source.a2s
import valve.source.messages

import serverstf
import serverstf.cache
import serverstf.cli
import serverstf.tags


log = logging.getLogger(__name__)


class PollError(Exception):
    """Exception raised for all polling errors."""


def _query_server(address):
    """Query the server info, players and rules.

    This issues a number of A2S queries to server identified by the given
    address.

    :param servers.cache.Address address: the address of the server to query.

    :raise PollError: if the server is unreachable or does not return a
        valid response.
    :return: a tuple containing the server info, players and rules as returned
        by a :class:`valve.source.a2s.ServerQuerier`.
    """
    query = valve.source.a2s.ServerQuerier(
        (str(address.ip), address.port), timeout=5)
    try:
        return query.get_info(), query.get_players(), query.get_rules()
    except valve.source.a2s.NoResponseError as exc:
        raise PollError("Timed out waiting for "
                        "response from {}".format(address)) from exc
    except NotImplementedError as exc:
        raise PollError("Compressed fragments; "
                        "couldn't poll {}".format(address)) from exc
    except valve.source.messages.BrokenMessageError as exc:
        raise PollError(
            "Seemingly broken response from {}".format(address)) from exc


def poll(tagger, geoip, address):
    """Poll the state of a server.

    This will issue a number of requests to the server at the given address
    to determine its current state. This state is then used to calculate the
    tags that should be applied to the server. The location of the server is
    also looked up in a GeoIP database.

    :param serverstf.tags.Tagger tagger: the tagger used to determine server
        tags.
    :param geoip2.database.Reader geoip: the MaxMind GeoIP2 database used to
        determine the geographic location of the server.
    :param servers.cache.Address address: the address of the server to poll.

    :return: a :class:`serverstf.cache.Status` containing the up-to-date
        state of the server.
    """
    log.debug("Polling %s", address)
    info, players, rules = _query_server(address)
    tags = tagger.evaluate(info, players, rules)
    location = geoip.city(str(address.ip))
    scores = []
    for entry in players["players"]:
        # For newly connected players there is a delay before their name
        # becomes available to the server, so we just filter these out.
        if entry["name"]:
            duration = datetime.timedelta(seconds=entry["duration"])
            scores.append((entry["name"], entry["score"], duration))
    players_status = serverstf.cache.Players(
        current=info["player_count"],
        max_=info["max_players"],
        bots=info["bot_count"],
        scores=scores,
    )
    return serverstf.cache.Status(
        address,
        interest=None,
        name=info["server_name"],
        map_=info["map"],
        application_id=info["app_id"],
        players=players_status,
        country=location.country.iso_code,
        latitude=location.location.latitude,
        longitude=location.location.longitude,
        tags=tags,
    )


def _interest_queue_iterator(cache):
    """Expose a cache's interest queue as an iterator.

    .. note::
        It is possible the generator returned by function can never be
        exhausted.
    """
    while True:
        try:
            with cache.interesting_context() as address:
                yield address
        except serverstf.cache.EmptyQueueError:
            return


def _watch(r_cache, w_cache, geoip, all_):
    """Poll servers in the cache.

    This will poll servers in the cache updating their statuses as it goes.
    Either the interest queue or entire cache is used to determining which
    servers to poll.

    Two separate caches using separate connections must be provided; one for
    reading addresses and one for writing the updates. This is to work around
    the fact that publishing updates to a cache creates a MULTI transaction
    which can interfere with the operations to read addresses from the cache.

    :param serverstf.cache.Cache r_cache: the server status cache to read
        addresses from.
    :param serverstf.cache.Cache w_cache: the server status cache to write
        updates to.
    :param geoip2.database.Reader geoip: the MaxMind GeoIP2 database used to
        determine the geographic location of the servers.
    :param bool all_: if ``True`` then every server in the cache will be
        polled. Otherwise only servers which exist in the internet queue
        will be.
    """
    log.info("Watching %s; all: %s", r_cache, all_)
    log.info("Writing to %s", w_cache)
    tagger = serverstf.tags.Tagger.scan(__package__)
    while True:
        if all_:
            addresses = r_cache.all_iterator()
        else:
            addresses = _interest_queue_iterator(r_cache)
        for address in addresses:
            try:
                status = poll(tagger, geoip, address)
            except PollError as exc:
                log.error("Couldn't poll %s: %s", address, exc)
            else:
                w_cache.set(status)


@serverstf.cli.subcommand("poller")
@serverstf.cli.geoip
@serverstf.cli.redis
@serverstf.cli.argument(
    "--all",
    action="store_true",
    help=("When set the poller will poll all servers "
          "in the cache, not only those in the interest queue."),
)
def _poller_main(args):
    """Continuously poll servers from the cache.

    Depending on whether ``--all`` was specified or not this will continuously
    poll servers from the interest queue or the cache in general. The updated
    status of each server is written to the cache.

    :raises serverstf.FatalError: if the GeoIP database cannot be loaded.
    """
    log.info("Starting poller")
    loop = asyncio.get_event_loop()
    try:
        log.info("Loading geoip database from %s", args.geoip)
        geoip = geoip2.database.Reader(str(args.geoip))
    except maxminddb.InvalidDatabaseError as exc:
        raise serverstf.FatalError(exc)
    else:
        with serverstf.cache.Cache.connect(args.redis, loop) as r_cache:
            with serverstf.cache.Cache.connect(args.redis, loop) as w_cache:
                _watch(r_cache, w_cache, geoip, args.all)
    finally:
        geoip.close()
    log.info("Stopping poller")


@serverstf.cli.subcommand("poll")
@serverstf.cli.argument(
    "address",
    type=serverstf.cache.Address.parse,
    help="The address of the server to poll in the <ip>:<port> form."
)
@serverstf.cli.geoip
def _poll_main(args):
    """Poll a server once.

    This will poll a given server once and print out its status. The status
    is *not* written to the cache.
    """
    geoip = geoip2.database.Reader(str(args.geoip))
    tagger = serverstf.tags.Tagger.scan(__package__)
    try:
        status = poll(tagger, geoip, args.address)
    except PollError as exc:
        raise serverstf.FatalError from exc
    else:
        players = sorted(status.players, key=lambda p: p[1], reverse=True)
        print("\nStatus\n------")
        print()
        print("Address:  ", status.address)
        print("Location: ", status.country, status.latitude, status.longitude)
        print("App:      ", status.application_id)
        print("Name:     ", status.name)
        print("Map:      ", status.map)
        print("Tags:     ")
        for tag in sorted(status.tags):
            print(" -", tag)
        print("Players:",
              "{0.current}/{0.max} ({0.bots} bots)".format(status.players))
        for name, score, duration in players:
            print(" -", str(duration).split(".")[0], str(score).rjust(4), name)
