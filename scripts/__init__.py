"""Narratarr's build and operations scripts.

This package holds code that is not part of the `narratarr` application
package. Refer to APP-CONTRACT.md section 14 for the file ownership map.

The application imports two modules from this package at run time:

- `scripts.espeak_guard`, the render-log check of APP-CONTRACT.md section
  11.2, point 2.
- `scripts.fetch_models`, the first-run model fetcher of section 11.1.

Both modules import cleanly with no heavy dependency loaded, so a test can
import this package without loading torch.
"""
