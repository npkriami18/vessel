from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from urllib.parse import urlparse

USERS = [
	{
		'id': 1,
		'email': 'alice@example.com',
		'name': 'Alice',
		'created_at': '2026-01-01T00:00:00Z',
	},
	{
		'id': 2,
		'email': 'bob@example.com',
		'name': 'Bob',
		'created_at': '2026-01-02T00:00:00Z',
	},
]


class Handler(BaseHTTPRequestHandler):
	def _send_json(self, status: int, payload: dict | list):
		body = json.dumps(payload).encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'application/json; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_GET(self):
		path = urlparse(self.path).path
		if path == '/health':
			return self._send_json(200, {'status': 'ok'})
		if path == '/users':
			return self._send_json(200, USERS)
		self._send_json(404, {'error': 'Not found'})


if __name__ == '__main__':
	server = HTTPServer(('127.0.0.1', 8000), Handler)
	print('Serving on http://127.0.0.1:8000')
	server.serve_forever()
