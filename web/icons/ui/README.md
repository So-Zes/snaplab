# Interface icons

One SVG file per icon. The file name (without `.svg`) is the icon's name in `web/app.js` (`icon("folder")`).

- Draw on a 24 x 24 `viewBox`.
- Use `currentColor` for strokes and fills, so the icon follows the text colour and the light/dark theme.
- The `<svg>` element may set `fill`, `stroke`, `stroke-width`, `stroke-linecap` and `stroke-linejoin`; they are kept.
- Only drawing elements: no scripts, no event attributes, no links to other files.

The server puts every file of this folder into the page as one hidden sprite. To change the look of the app,
replace the files here (keep the names) and reload. `python -m unittest discover -s tests -p test_round6.py`
checks that every icon the app uses has a file.

Settings can switch to PNG icons in `../soft3d/<name>.png`, with a matching dark variant in
`../soft3d/dark/<name>.png`. Keep all three sets of names aligned. The default remains SVG; small status
and metadata marks stay SVG in both modes. `test_round7.py` checks the 57 matching sets.
