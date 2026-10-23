from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from .engine import Engine


class Handler(BaseHTTPRequestHandler):
    engine = None
    token = None

    def log_message(self, *args):
        # Default HTTP logs include paths and may expose credential query strings.
        pass

    def respond(self, status, value, mime='application/json; charset=utf-8'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, default=str, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        supplied = self.headers.get('Authorization', '')
        return bool(self.token) and hmac.compare_digest(supplied, 'Bearer ' + self.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == '/health':
                self.engine.store.one('SELECT 1')
                return self.respond(200, {'status': 'healthy', 'version': '0.3.1', 'mode': 'paper'})
            if path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
                return self.respond(200, (self.engine.root / 'web' / name).read_bytes(), mime)
            if path == '/v1/status':
                return self.respond(200, self.engine.status())
            if path == '/v1/report':
                return self.respond(200, {'text': self.engine.report()})
            if path in ('/llm/v1/models', '/llm/models') and self.authenticated():
                model = self.engine.settings['model']
                return self.respond(200, {'object': 'list', 'data': [{'id': model, 'object': 'model', 'owned_by': 'configured-openrouter-provider'}]})
            return self.respond(404, {'error': 'not_found'})
        except Exception as error:
            return self.respond(503, {'error': type(error).__name__})

    def do_POST(self):
        if not self.authenticated():
            return self.respond(401, {'error': 'authentication_required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 250000:
                raise ValueError('request_size_invalid')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('object_required')
            path = urlsplit(self.path).path
            if path.startswith('/v1/jobs/'):
                result = self.engine.run(path.removeprefix('/v1/jobs/'))
            elif path == '/v1/research/claim':
                result = self.engine.claim(str(data.get('purpose', 'scan')))
            elif path == '/v1/research/complete':
                result = self.engine.complete(data['run_id'], data['result'])
            elif path == '/v1/research/decision':
                result = self.engine.stage_decision(data['run_id'], data['decision'])
            elif path == '/v1/tools':
                result = {'result': self.engine.tool(str(data['run_id']), str(data['name']))}
            elif path in ('/llm/v1/chat/completions', '/llm/chat/completions'):
                streaming = data.get('stream') is True
                result = self.engine.budget.completion({**data, 'stream': False})
                if streaming:
                    choices = []
                    for choice in result.get('choices', []):
                        message = dict(choice.get('message', {}))
                        if message.get('tool_calls'):
                            message['tool_calls'] = [{**tool, 'index': i} for i, tool in enumerate(message['tool_calls'])]
                        choices.append({'index': choice.get('index', 0), 'delta': message, 'finish_reason': None})
                    chunk = {'id': result.get('id'), 'object': 'chat.completion.chunk', 'created': result.get('created'),
                             'model': result.get('model'), 'choices': choices}
                    final = {**chunk, 'choices': [{'index': c.get('index', 0), 'delta': {}, 'finish_reason': c.get('finish_reason', 'stop')} for c in result.get('choices', [])],
                             'usage': result.get('usage')}
                    body = 'data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(final) + '\n\ndata: [DONE]\n\n'
                    return self.respond(200, body.encode(), 'text/event-stream')
            else:
                return self.respond(404, {'error': 'not_found'})
            return self.respond(200, result)
        except (KeyError, ValueError) as error:
            return self.respond(400, {'error': str(error)[:120] if type(error) is ValueError else 'required_field_missing'})
        except RuntimeError as error:
            # RuntimeError messages here are fixed application codes, not upstream bodies.
            return self.respond(409, {'error': str(error)[:120]})
        except Exception as error:
            return self.respond(503, {'error': type(error).__name__})


def main():
    os.umask(0o077)
    token = os.environ.get('HUB_TOKEN', '')
    if len(token) < 32:
        raise RuntimeError('HUB_TOKEN must be configured privately')
    for _ in range(30):
        try:
            engine = Engine()
            break
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError('research database could not be initialized')
    Handler.engine, Handler.token = engine, token
    print('Research hub ready: paper mode, authenticated jobs, shared AI accounting', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8766), Handler).serve_forever()


if __name__ == '__main__':
    main()
