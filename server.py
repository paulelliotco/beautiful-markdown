import sys
import os

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from docs_converter import DocsConverter

app = Flask(__name__)

# Configure CORS with explicit headers
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "expose_headers": ["Content-Type", "Authorization"]
    }
})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return render_template('index.html')
        
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                'error': 'Invalid request',
                'details': 'Request body must be valid JSON'
            }), 400

        url = data.get('url')
        gemini_api_key = data.get('geminiApiKey')

        if not url:
            return jsonify({
                'error': 'URL is required',
                'example': {
                    'url': "https://docs.example.com/",
                    'geminiApiKey': "optional-api-key"
                }
            }), 400

        # Validate URL format
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if not all([parsed.scheme, parsed.netloc]):
                return jsonify({
                    'error': 'Invalid URL',
                    'details': 'URL must include protocol (e.g., https://) and domain'
                }), 400
            if parsed.scheme not in ['http', 'https']:
                return jsonify({
                    'error': 'Invalid URL',
                    'details': 'Only HTTP and HTTPS protocols are supported'
                }), 400
        except Exception:
            return jsonify({
                'error': 'Invalid URL',
                'details': 'Could not parse the provided URL'
            }), 400

        # Validate Gemini API key if provided
        if gemini_api_key:
            if not isinstance(gemini_api_key, str) or len(gemini_api_key) < 10:
                return jsonify({
                    'error': 'Invalid Gemini API key',
                    'details': 'API key must be a valid string'
                }), 400

        converter = DocsConverter(
            base_url=url,
            max_depth=1,  # Always set to 1
            gemini_api_key=gemini_api_key
        )
        
        try:
            markdown = converter.convert()
            if not markdown:
                return jsonify({
                    'error': 'Failed to convert documentation',
                    'details': 'Could not fetch or process the documentation'
                }), 400

            return markdown
        except Exception as e:
            return jsonify({
                'error': 'Conversion failed',
                'details': str(e)
            }), 400

    except Exception as e:
        return jsonify({
            'error': 'Server error',
            'details': str(e)
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
