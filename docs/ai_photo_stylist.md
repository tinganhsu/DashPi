# AI Photo Stylist Plugin

AI Photo Stylist is a built-in DashPi plugin that restyles your own photos with Google Gemini image models. It is separate from Image Upload and AI Image: it has its own upload folder, its own settings, and its own generated-image cache.

## What It Does

- Upload photos inside the AI Photo Stylist settings page.
- Select one uploaded photo, or let the plugin pick one randomly.
- In random mode, prioritize photos that have not been styled yet, then unused styles for each photo.
- Optionally include cached generated images in the random photo pool and display them directly when selected.
- Select a style from `vibe-pic.json`, or let the plugin pick one randomly.
- Generate a new styled image with Gemini.
- Save generated results to a local cache.
- If Gemini returns an API error later, show a random cached result instead of failing the display update.

## Required API Key

Add your Gemini API key in **Settings > API Keys**:

```text
GOOGLE_GEMINI_SECRET
```

The plugin uses Gemini native image models:

- `gemini-2.5-flash-image`
- `gemini-3-pro-image-preview`
- `gemini-3.1-flash-image-preview`

## File Locations

AI Photo Stylist uses private folders under `src/static/images/ai_photo_stylist/`:

```text
src/static/images/ai_photo_stylist/uploads/
src/static/images/ai_photo_stylist/cached/
src/static/images/ai_photo_stylist/style_usage.json
```

- `uploads/` stores source photos uploaded through this plugin.
- `cached/` stores Gemini-generated PNG outputs.
- `style_usage.json` stores the random-selection history for successful source photo and style combinations.
- These folders are not shared with the Image Upload plugin.

The style prompt file lives here:

```text
src/plugins/ai_photo_stylist/resources/vibe-pic.json
```

## `vibe-pic.json` Format

`vibe-pic.json` should be a JSON array. Each style needs a name and prompt. The plugin accepts either `style_name` or `name`.

```json
[
  {
    "style_name": "浮世繪風格 (Ukiyo-e)",
    "prompt": "Traditional Japanese ukiyo-e woodblock print style..."
  },
  {
    "name": "Cinematic Portrait",
    "prompt": "Restyle the input photo as a cinematic portrait..."
  }
]
```

An optional `id` field may be provided. If omitted, DashPi derives one from the style name.

## Settings

- **Source Photo**: choose one uploaded photo.
- **Random Photo**: pick an uploaded photo each refresh, prioritizing photos with no generated styles yet.
- **Include Cache in Random**: when Random Photo is enabled, also include generated cache files in the random pool. If a cached file is selected, DashPi displays it directly and skips Gemini generation.
- **Vibe**: choose one style from `vibe-pic.json`.
- **Random Vibe**: pick a style each refresh, prioritizing styles that have not yet been used for the selected source photo.
- **Extra Prompt**: append extra instructions to the selected vibe prompt.
- **Image Model**: choose the Gemini image model.
- **Fit Mode**: fit with letterboxing or fill by cropping.
- **Show Caption**: overlay the source filename and vibe name.

## Random Selection Behavior

When both Random Photo and Random Vibe are enabled, AI Photo Stylist tracks successful source photo and style combinations. It first chooses from uploaded photos that have never been styled. After every uploaded photo has at least one result, it chooses photos that still have unused styles. For the selected photo, Random Vibe chooses from styles that have not yet been used with that photo.

After every uploaded photo has been generated with every available style, the usage history for the current uploaded photos resets and the cycle starts again. The history is stored in `src/static/images/ai_photo_stylist/style_usage.json`, not in `device.json`, so the main DashPi config stays small. DashPi's normal update flow uses the existing source tree and does not overwrite this ignored runtime file.

The history is updated only after Gemini successfully generates and saves a new cached image. Cached images shown directly through Include Cache in Random do not change this history.

## Cache Fallback

When generation succeeds, the plugin saves the generated image into `cached/`.

When Gemini generation fails because of an API or SDK error, DashPi logs the exception and tries to display a random image from `cached/`. If the cache is empty, the plugin raises an error so the Web UI can show what went wrong.

## Notes

- Deleting source photos from the settings page only removes files from `uploads/`.
- Cached generated images are kept so fallback remains useful.
- To clear generated results manually, remove files from `src/static/images/ai_photo_stylist/cached/`.
- To reset random photo/style history manually, remove `src/static/images/ai_photo_stylist/style_usage.json`.
