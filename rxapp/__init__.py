"""rxapp: a browser SDR and monitoring app for the RX-888, built on ka9q-radio's radiod.

radiod owns the radio and does the channelizing; rxapp is its only client. It creates and
tunes channels, relays their audio and the spectrum to the browser, runs scans and
recordings, and keeps a log in SQLite.
"""
