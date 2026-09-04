/* Copyright (C) 2026 Kukni contributors */
/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Canon CR2 preview support for GNOME Sushi 46. */

const {GdkPixbuf, Gio, GLib, GObject} = imports.gi;

const ImageViewer = imports.viewers.image;

const EXTRACTION_TIMEOUT_SECONDS = 5;
const READ_CHUNK_SIZE = 64 * 1024;
const MAX_RENDER_DIMENSION = 4096;
const MAX_SOURCE_DIMENSION = 32768;
const MAX_SOURCE_PIXELS = 100000000;

var Klass = GObject.registerClass(
class RawImageRenderer extends ImageViewer.Klass {
    _createImageTexture(file) {
        this._rawPreviewDestroyed = false;
        this._rawPreviewErrorReported = false;
        this._rawPreviewFinished = false;
        this._rawPreviewProcess = null;
        this._rawPreviewStream = null;
        this._rawPreviewLoader = null;
        this._rawPreviewTimeoutId = 0;
        this._rawPreviewDimensionError = null;

        const filePath = file.get_path();
        if (!filePath) {
            super._createImageTexture(file);
            return;
        }

        const helper = GLib.build_filenamev([
            GLib.get_user_data_dir(),
            'sushi',
            'helpers',
            'kukni-extract-preview.py',
        ]);

        try {
            const process = Gio.Subprocess.new(
                [helper, filePath],
                Gio.SubprocessFlags.STDOUT_PIPE |
                Gio.SubprocessFlags.STDERR_SILENCE
            );
            this._rawPreviewProcess = process;
            this._rawPreviewStream = process.get_stdout_pipe();

            // Do not attach the UI cancellable here. This callback must always
            // finish so GLib can reap the child after cancellation or teardown.
            process.wait_check_async(null, (waitedProcess, result) => {
                this._rawPreviewFinished = true;
                this._clearRawPreviewTimeout();
                try {
                    waitedProcess.wait_check_finish(result);
                } catch (error) {
                    this._failRawPreview(error);
                }
                if (this._rawPreviewProcess === waitedProcess)
                    this._rawPreviewProcess = null;
            });

            this._rawPreviewTimeoutId = GLib.timeout_add_seconds(
                GLib.PRIORITY_DEFAULT,
                EXTRACTION_TIMEOUT_SECONDS,
                () => {
                    this._rawPreviewTimeoutId = 0;
                    if (!this._rawPreviewFinished && !this._rawPreviewDestroyed)
                        this._failRawPreview(new Error('CR2 preview extraction timed out'));
                    return GLib.SOURCE_REMOVE;
                }
            );

            this._decodeRawPreview();
        } catch (error) {
            this._failRawPreview(error);
        }
    }

    _decodeRawPreview() {
        const loader = new GdkPixbuf.PixbufLoader();
        this._rawPreviewLoader = loader;

        loader.connect('size-prepared', (_loader, width, height) => {
            if (width <= 0 || height <= 0 ||
                width > MAX_SOURCE_DIMENSION ||
                height > MAX_SOURCE_DIMENSION ||
                width * height > MAX_SOURCE_PIXELS) {
                this._rawPreviewDimensionError =
                    new Error('Embedded CR2 preview dimensions exceed the safe limit');
            }

            if (width > 0 && height > 0) {
                const scale = Math.min(
                    1,
                    MAX_RENDER_DIMENSION / width,
                    MAX_RENDER_DIMENSION / height
                );
                if (scale < 1) {
                    loader.set_size(
                        Math.max(1, Math.floor(width * scale)),
                        Math.max(1, Math.floor(height * scale))
                    );
                }
            }
        });

        this._readRawPreviewChunk();
    }

    _readRawPreviewChunk() {
        const stream = this._rawPreviewStream;
        if (!stream)
            return;

        stream.read_bytes_async(
            READ_CHUNK_SIZE,
            GLib.PRIORITY_DEFAULT,
            this._cancellable,
            (source, result) => {
                let bytes;
                try {
                    bytes = source.read_bytes_finish(result);
                } catch (error) {
                    if (!this._rawPreviewDestroyed)
                        this._failRawPreview(error);
                    return;
                }

                if (this._rawPreviewDestroyed)
                    return;

                try {
                    if (bytes.get_size() === 0) {
                        const loader = this._rawPreviewLoader;
                        if (!loader)
                            return;
                        loader.close();
                        const pixbuf = loader.get_pixbuf();
                        if (!pixbuf)
                            throw new Error('Embedded CR2 preview could not be decoded');
                        this._rawPreviewLoader = null;
                        this._closeRawPreviewStream();
                        this._setPix(pixbuf.apply_embedded_orientation());
                        return;
                    }

                    if (!this._rawPreviewLoader.write_bytes(bytes))
                        throw new Error('Embedded CR2 preview could not be decoded');
                    if (this._rawPreviewDimensionError)
                        throw this._rawPreviewDimensionError;
                    this._readRawPreviewChunk();
                } catch (error) {
                    this._failRawPreview(error);
                }
            }
        );
    }

    _failRawPreview(error) {
        if (this._rawPreviewDestroyed)
            return;

        if (!this._rawPreviewErrorReported) {
            this._rawPreviewErrorReported = true;
            this.emit('error', error);
        }
        this._discardRawPreviewLoader();
        this._closeRawPreviewStream();
        this._stopRawPreviewProcess();
    }

    _discardRawPreviewLoader() {
        const loader = this._rawPreviewLoader;
        if (!loader)
            return;
        this._rawPreviewLoader = null;
        try {
            loader.close();
        } catch (_error) {
            // An incomplete image is expected on cancellation or decode error.
        }
    }

    _closeRawPreviewStream() {
        const stream = this._rawPreviewStream;
        if (!stream)
            return;
        this._rawPreviewStream = null;
        stream.close_async(GLib.PRIORITY_DEFAULT, null, (source, result) => {
            try {
                source.close_finish(result);
            } catch (error) {
                if (!this._rawPreviewDestroyed)
                    logError(error, 'Unable to close the CR2 preview stream');
            }
        });
    }

    _stopRawPreviewProcess() {
        if (!this._rawPreviewProcess || this._rawPreviewFinished)
            return;
        try {
            this._rawPreviewProcess.force_exit();
        } catch (error) {
            if (!this._rawPreviewDestroyed)
                logError(error, 'Unable to stop the CR2 preview helper');
        }
    }

    _clearRawPreviewTimeout() {
        if (!this._rawPreviewTimeoutId)
            return;
        GLib.source_remove(this._rawPreviewTimeoutId);
        this._rawPreviewTimeoutId = 0;
    }

    _onDestroy() {
        this._rawPreviewDestroyed = true;
        this._clearRawPreviewTimeout();
        super._onDestroy();
        this._discardRawPreviewLoader();
        this._closeRawPreviewStream();
        this._stopRawPreviewProcess();
    }
});

var mimeTypes = [
    'image/x-canon-cr2',
    'image/x-cr2',
];
