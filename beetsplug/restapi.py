import base64
from collections import defaultdict
import io
import json
import os

import flask
from beets import ui, library, util
from beets.plugins import BeetsPlugin
from flask import g, jsonify, request
from mediafile import MediaFile
from PIL import Image as PillowImage
import sys
from beets.ui.commands import import_cmd
from beets.importer import ImportTask, ImportSession

app = flask.Flask(__name__)


def _to_json(obj):
    out = dict(obj)

    if isinstance(obj, library.Item):
        out['path'] = util.displayable_path(out['path'])

        for key, value in out.items():
            if isinstance(out[key], bytes):
                out[key] = base64.b64encode(value).decode('ascii')

        try:
            out['size'] = os.path.getsize(util.syspath(obj.path))
        except OSError:
            out['size'] = 0

        return out

    elif isinstance(obj, library.Album):
        out['artpath'] = util.displayable_path(out['artpath'])
        return out


def _json_list(items):
    yield '['
    first = True
    for item in items:
        if first:
            first = False
        else:
            yield ','
        yield json.dumps(_to_json(item))
    yield ']'


def create_thumbnail(image_data, size):
    if len(image_data) == 0:
        return b''

    try:
        pil_image = PillowImage.open(io.BytesIO(image_data))
        pil_image.thumbnail((size, size), PillowImage.ANTIALIAS)

        image_bytes = io.BytesIO()
        pil_image.save(image_bytes, pil_image.format)
        image_bytes.seek(0)

        return image_bytes.getvalue()
    except Exception:
        return b''


@app.before_request
def before_request():
    g.lib = app.config['lib']
    g.plug = app.config['plugin']

def get_lib() -> library.Library:
    return g.lib

def get_plugin(): # type: () -> RestApiPlugin
    return g.plug

@app.route('/items', methods=["GET"])
def item_query():
    query = request.args.get('query')
    result = get_lib().items(query)
    return app.response_class(_json_list(result), mimetype='application/json')


@app.route('/item/<int:item_id>/file')
def item_file(item_id):
    item = get_lib().get_item(item_id)

    if not item:
        return flask.abort(404)

    item_path = util.py3_path(item.path)

    try:
        unicode_item_path = util.text_string(item.path)
    except (UnicodeDecodeError, UnicodeEncodeError):
        unicode_item_path = util.displayable_path(item.path)

    base_filename = os.path.basename(unicode_item_path)
    response = flask.send_file(item_path, as_attachment=True, attachment_filename=base_filename )
    response.headers['Content-Length'] = os.path.getsize(item_path)
    return response


@app.route('/item/<int:item_id>/art')
def item_art(item_id):
    size = request.args.get('size', type=int)
    item = get_lib().get_item(item_id)

    if not item:
        return flask.abort(404)

    item_path = util.py3_path(item.path)
    metadata = MediaFile(item_path)

    if len(metadata.images) == 0:
        return flask.abort(404)

    image = metadata.images[0].data

    if size and 0 < size < 1600:
        image = create_thumbnail(image, int(size))

    response = flask.send_file(io.BytesIO(image), as_attachment=True, attachment_filename="art.jpg", mimetype='image/jpeg')
    response.headers['Content-Length'] = len(image)
    return response

@app.route('/import', methods=["PUT"])
def import_path():
    plugin = get_plugin()
    plugin.reset() # reset the import results list 
    body = request.get_json() # type: dict
    path = body.get("path")
    if not path:
        return flask.abort(400, {'message': '"path" is required' })
    args = [x for x in body.get("args", "").split(" ") if x] # Filter empty string

    subopts, subargs = import_cmd.parser.parse_args(args + [path])
    # We *must* run in quiet mode, otherwise import will block the terminal forever.
    subopts.ensure_value('quiet', True)
    import_cmd.func(get_lib(), subopts, subargs)

    return jsonify(ok=True, summary=plugin.action_counts, details=plugin.import_choices)


class RestApiPlugin(BeetsPlugin):

    def __init__(self):
        super(RestApiPlugin, self).__init__()

        self.reset()

        self.config.add({
            'host': u'127.0.0.1',
            'port': 8338
        })
        self.register_listener('import_task_choice', func=self.record_import_choice)

    def reset(self): 
        self.import_choices = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        self.action_counts = defaultdict(lambda: 0)

    def record_import_choice(self, session: ImportSession, task: ImportTask):
        items = task.items # type: list[library.Item]
        for i in items:
            self.import_choices[task.choice_flag.name][i.albumartist][i.album].append({
                "artist": i.albumartist, 
                "album": i.album, 
                "title": i.title, 
                "path": i.path.decode()
            })
            self.action_counts[task.choice_flag.name] += 1

    def commands(self):
        cmd = ui.Subcommand('restapi', help=u'start a REST api server')

        def func(lib, opts, args):
            args = ui.decargs(args)

            if args:
                self.config['host'] = args.pop(0)
            if args:
                self.config['port'] = int(args.pop(0))

            app.config['lib'] = lib
            app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False
            app.config['log'] = self._log
            app.config['plugin'] = self

            app.run(host=self.config['host'].as_str(),
                    port=self.config['port'].get(int),
                    debug=False)

        cmd.func = func
        return [cmd]
