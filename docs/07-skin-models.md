# 07 · The clinic's skin models

The app can use the clinic's two skin models, which run as WebSocket services on the model server:

| Model | What it does | Default address | Project |
|---|---|---|---|
| Skin diagnosis (Detectron2 Mask R-CNN) | Finds acne, scars, pigmentation and dark circles on the face and marks them on the photo | `ws://192.168.0.84:9601/ws/cv/9819628774/image/image` | 187 |
| Skin post-treatment (Detectron2 + LaMa) | Finds the same spots and paints them out: a picture of the face "after" | `ws://192.168.0.84:9600/ws/cv/3271585349/image/image` | 167 |

## Switching it on

In `backend/.env`:

```
ANALYSIS_ENGINE=ws
SKIN_DIAGNOSIS_WS_URL=ws://192.168.0.84:9601/ws/cv/9819628774/image/image
SKIN_POST_TREATMENT_WS_URL=ws://192.168.0.84:9600/ws/cv/3271585349/image/image
```

**Through the model gateway** (192.168.0.84:5007), the addresses carry the project id and a token:

```
SKIN_DIAGNOSIS_WS_URL=ws://192.168.0.84:5007/ws/models/187/ws/cv/1001/image/image?token=&notify=1
SKIN_POST_TREATMENT_WS_URL=ws://192.168.0.84:5007/ws/models/167/ws/cv/1001/image/image?token=&notify=1
SKIN_MODEL_AUTH_EMAIL=<platform service account>     # recommended; or leave empty and open the app from WorkflowHub
SKIN_MODEL_AUTH_PASSWORD=<its password>
```

The gateway takes a platform access token, and those last 15 minutes, so the app keeps one renewed. It comes from a
service account, or from the platform session of the last staff member who signed in through WorkflowHub, renewed
with the platform's refresh endpoint. See [08-platform-sso.md](08-platform-sso.md).

- **Where the token goes:** the app puts it into `token=`, or into a `{token}` placeholder, only for addresses that
  ask for one, and keeps it out of every log and error message.
- **Refused token:** a 401 or 403 from the gateway gets one retry with a renewed token, then a message saying how to
  give the app a session.
- **Gateway messages:** progress messages the gateway sends with `notify=1` are skipped; an error message from it is
  reported.

Restart the API. `GET /api/health` shows `"analysis_engine": "ws"` and which models are configured.

The parameters sent to each service default to the clinic's (`SKIN_DIAGNOSIS_PARAMS`, `SKIN_POST_TREATMENT_PARAMS`). The app sends the object inside `"parameters"` and adds `"return_predictions": true`. The post-treatment service deployed on 26 October fails on the wrapped form and sends the photo back unchanged; the bare form works on every version. A photo sent back unchanged is treated as a failure.

Checked on 26 October 2026 through the gateway at 192.168.0.84:5007: the diagnosis model (187) returned a marked-up 512×512 face crop in about a second per photo (acne, scars, dark circles), and the post-treatment model (167) returned the spots painted out in about 4 s. Neither sends findings yet, so until both are redeployed with the changes below, the scores stay estimates and the client isn't shown the pictures.

The other settings:

| Setting | Default | Meaning |
|---|---|---|
| `SKIN_MODEL_AUTH_EMAIL`, `SKIN_MODEL_AUTH_PASSWORD` | (none) | Service account for the model gateway (see 08) |
| `SKIN_MODEL_TOKEN_FROM_SSO` | true | Otherwise use the platform session from staff SSO sign-ins |
| `SKIN_MODEL_TOKEN` | (none) | A fixed gateway token, for quick checks only (it expires) |
| `SKIN_MODEL_CONNECT_TIMEOUT_S` | 5 | Time allowed to connect to a model server |
| `SKIN_DIAGNOSIS_TIMEOUT_S` | 60 | Time allowed per photo for the diagnosis model |
| `SKIN_POST_TREATMENT_TIMEOUT_S` | 300 | Time allowed for the preview; LaMa inpainting is slow on a CPU |
| `SKIN_MODEL_UNWRAP_PARAMS` | true | Send the parameters' inner object rather than the `{"parameters": …}` wrapper |
| `SKIN_MODEL_DETAILS_WAIT_S` | 1 | How long to wait after the image for the findings message; older services never send one |
| `SKIN_MODEL_RETRY_AFTER_S` | 60 | After a server fails, how long it is skipped instead of being waited on again |

## Protocol

The flow is the same for both services:

1. Send the parameters as a JSON text message.
2. Send the photo as bytes.
3. The service answers with an image (JPEG bytes).
4. If the parameters carry `"return_predictions": true`, an updated service then sends one more text message:

```json
{"type": "predictions", "model": "skin-diagnosis", "face_detected": true,
 "face_box": [x1, y1, x2, y2], "input_size": [w, h], "image_size": [w, h], "output_size": [w, h],
 "detections": [{"class": "acne", "score": 0.83, "bbox": [x1, y1, x2, y2], "area_px": 412}],
 "inpainted": true}
```

In that message:
- `face_box` is in the coordinates of the photo that was sent.
- `bbox` and `area_px` are in the coordinates of the face crop the model analysed (`image_size`).
- `inpainted` is sent by the post-treatment service only.

Clients that don't ask for findings get exactly the old behaviour: one image per photo.

### Changes made in the two service folders

The two folders are `detectron2_diagnose_updated` and `skin_post_treatment`. The originals are kept in each folder's `backup-before-app-integration/`.

- **`utils/utils.py`** (identical in both):
  - Accepts the parameters either bare or wrapped in `{"parameters": {...}}`. Before, a wrapped request silently fell back to the defaults.
  - Sends the findings message when asked.
  - Stops cleanly when the client disconnects.
- **`prediction.py`** (both):
  - Adds `stream_with_details()`; `stream()` is unchanged for existing callers.
  - Uses the largest face rather than the first one found.
  - Records the mask area of each finding.
- **Diagnosis service:**
  - Fixes the resize call. `cv2.resize(crop, params.get('output_image_size'), (1024,1024))` passed the default as `dst`, so requests without `output_image_size` failed.
  - Records findings even with `draw_bbox` off.

**Redeploy both services** on the model server for the app to get findings. Until then, the app keeps the pictures but treats the scores as estimates. See below.

## What the app does with it

When the third photo is accepted, the analysis runs:

1. **Diagnosis.** Each photo (front, left, right) goes to the diagnosis model. Each answer is stored as an overlay picture.
2. **Scores.** Acne, pigmentation and dark circles are scored 0–100 from the model's findings. These scores are tagged *model* in the doctor's review.
   - The score combines the number of spots, their confidence and the area of the face they cover.
   - Each concern takes its worst photo, so a spot seen from two angles isn't counted twice.
   - Only classes the service was asked to look for are scored.
3. **Scars** are listed for the doctor ("Also found") but aren't scored, because the app doesn't track scars as a concern.
4. **Estimates.** Wrinkles, redness, pores, dullness and dryness stay *estimates*, because no model covers them yet.
5. **Preview.** The front photo goes to the post-treatment model in the background. The picture appears in the doctor's review when it's ready, which can take minutes.

The scoring weights are a first calibration and are in one place: `app/integrations/skin_models.py`, `concern_score`. **They need checking by the clinic's doctors against their own grading on real cases before clinical use.**

## Who sees what

| | Doctor / admin (review) | Clinic device, client's phone, shared link |
|---|---|---|
| Model scores (tagged model / estimate) | Always | Findings as before |
| Marked-up photos | Always | Only after the plan is approved |
| Post-treatment preview | Always, with its status; **Try again** if it failed | Only after approval, labelled **"Illustration only"**: *not a promise of results* |

The client is never shown a picture:
- from a service that didn't report its findings, because the app can't confirm it's the right model;
- of a photo where the model found no face;
- of a preview that painted nothing out;
- of a preview that isn't the size asked for, for example another model answering on that address.

The doctor sees each of these with a warning that says why.

The chat assistant is told the scores and whether they came from the model, never about the preview.

Picture files: `GET /api/model-images/{id}/file`. Add `?variant=before` for the untreated face crop shown beside the preview.
- **Doctors and admins** can open any picture.
- **Operators** get the client's rules.
- **The client** uses `GET /api/me/model-images/{id}/file`.
- **A shared summary link** uses `GET /api/public/summaries/{token}/images/{id}`.

Anything not allowed answers 404.

## When a model server is down

The consultation never waits on a model:
- **Diagnosis down:** one quick failed connection, then the whole analysis uses the estimates. The doctor's review says the model was unreachable.
- **Preview down:** the preview is marked failed with the reason. The doctor can press **Try again** later, or call `POST /api/visits/{id}/model-images/preview`.
- **After a failure,** the server is skipped for `SKIN_MODEL_RETRY_AFTER_S`.

## Without the model server: the stand-in

`scripts/fake_skin_models.py` speaks the same protocol, including the findings message, for development and demos. It is **not a model**: it marks dark or reddish spots with simple image processing and paints them out with OpenCV. Every picture it makes says "STAND-IN MODEL". Don't use it for real clients.

```
python scripts/fake_skin_models.py                 # diagnosis :9711, post-treatment :9710
python scripts/fake_skin_models.py --no-face-check # synthetic test photos without a real face
```

Then point `SKIN_DIAGNOSIS_WS_URL` at `ws://127.0.0.1:9711/ws/cv/1/image/image` and `SKIN_POST_TREATMENT_WS_URL` at `ws://127.0.0.1:9710/ws/cv/2/image/image`.

## Tests

- **`backend/tests/test_skin_models.py`** checks, against fake model servers:
  - scores, overlays and the preview;
  - who sees what before and after approval;
  - another client's phone;
  - shared links;
  - older services without findings;
  - servers that are down, then retried;
  - a preview that changed nothing;
  - a wrong-size reply;
  - no face;
  - that the chat agent isn't told about the preview.
- **`e2e/models.mjs`** runs the same journey in a real browser: doctor review, clinic device before and after approval, and the client's phone.
