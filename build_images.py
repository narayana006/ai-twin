"""Build the Modal images one at a time (accounts with low limits kill parallel builds).
    .venv/bin/python build_images.py [web brain lipsync]
Then `modal deploy modal_app.py` reuses the cached images."""
import sys
import time

import modal

import modal_app as m

IMAGES = {"web": m.web_image, "brain": m.brain_image, "lipsync": m.lipsync_image, "avatar": m.avatar_image, "video": m.video_image,
          "dream": m.dream_image}
build_app = modal.App.lookup("twin-build", create_if_missing=True)

for name in sys.argv[1:] or IMAGES:
    for attempt in range(1, 4):
        t = time.time()
        try:
            with modal.enable_output():
                IMAGES[name].build(build_app)
            print(f"BUILT {name} in {time.time() - t:.0f}s", flush=True)
            break
        except Exception as e:
            print(f"FAILED {name} attempt {attempt}: {e}", flush=True)
    else:
        sys.exit(f"GAVE UP on {name}")
