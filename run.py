"""Local dev entrypoint: python run.py → http://localhost:5001

Port 5001, not 5000: macOS binds 5000 to the AirPlay Receiver by default.
"""
import os

from app import create_app, start_background_jobs

USE_RELOADER = os.environ.get('USE_RELOADER', '1') == '1'

app = create_app()

# The reloader runs two processes. Only the child (WERKZEUG_RUN_MAIN=true) owns
# the scheduler, or every start/stop job would be armed twice.
if not USE_RELOADER or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    start_background_jobs(app)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5001)),
            debug=True, use_reloader=USE_RELOADER)
