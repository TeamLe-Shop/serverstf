"""The Pyramid application that serves the user interface."""

import logging

import pyramid.config
import waitress

import serverstf


log = logging.getLogger(__name__)


def _make_application():
    config = pyramid.config.Configurator()
    config.add_route("main", "/")
    config.add_view(
        lambda r: "hello world",
        route_name="main",
        renderer="string",
    )
    return config.make_wsgi_app()


def _main_ui_args(parser):
    parser.add_argument(
        "port",
        type=int,
        help="The port the UI server will listen on.",
    )
    parser.add_argument(
        "--development",
        action="store_true",
        help=("Enable extra debugging features "
              "which are not safe for production use."),
    )


@serverstf.subcommand("ui", _main_ui_args)
def _main_ui(args):
    application = _make_application()
    kwargs = {
        "port": args.port,
        "threads": 1,
    }
    if args.development:
        kwargs["expose_tracebacks"] = True
    log.info("Serving UI on port %i", args.port)
    waitress.serve(application, **kwargs)
