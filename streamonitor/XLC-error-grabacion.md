# XLoveCam (XLC) · Fallos de grabación con FFmpeg

Incidencia pendiente de investigar. Observada en los registros del 11 y 12 de noviembre de 2026.

## Fallo durante la grabación

En las grabaciones de `matureedesiree` y `kellydirty`, FFmpeg ya había recibido varios minutos de video cuando reportó paquetes MPEG-TS corruptos. Después falló al interpretar la cabecera ADTS del audio AAC y al escribirlo en el MP4.

```text
PES packet size mismatch
Packet corrupt
Error parsing ADTS frame header!
Error applying bitstream filters to an output packet for stream #1: Invalid data found when processing input
Error muxing a packet
Conversion failed!
The process exited with an error. Return code: 3199971767 (-1094995529)
Recording ended with error
Error on downloading
```

En el caso de `kellydirty` del 11 de noviembre a las 22:42, el registro también mostró un fallo TLS, el error de conexión `-10054` y un reintento de un segmento HLS antes de los errores de audio. Queda por verificar si la interrupción de red dejó un segmento incompleto o si el segmento ya venía dañado.

## Fallo al abrir la playlist

El 12 de noviembre a las 14:48, `kellydirty` apareció en línea y FFmpeg falló al abrir la entrada HLS. El código de salida fue el mismo, aunque el fallo ocurrió antes de empezar a escribir audio o video. El registro no incluye el código HTTP ni el contenido que recibió FFmpeg.

```text
Error opening input: Invalid data found when processing input
Error opening input files: Invalid data found when processing input
```

## Impacto y comprobación pendiente

En una revisión de `D:\Pruebas\downloads` el 12 de noviembre, FFprobe no pudo abrir 8 de 12 MP4 de XLoveCam. Los archivos afectados pertenecían a `nicolememphis`, `kellydirty` y `matureedesiree`. FFprobe reportó `moov atom not found`. Esto confirma que faltan metadatos necesarios para abrir esos archivos. Queda por determinar si cada MP4 falló por el mismo error de audio.

Para retomar la investigación, revisar [la selección de la playlist de XLoveCam](sites/xlovecam.py) y [la captura directa con FFmpeg](downloaders/ffmpeg.py). Correlacionar el archivo `.stderr.log` de cada intento con su MP4 y reproducir los dos fallos por separado. La corrección debe comprobar con FFprobe que, si falla una entrada después de recibir video, el archivo guardado se puede abrir y conserva las pistas recuperables.

## Bloques completos del registro

### matureedesiree · 2026-11-11 22:36:13

```text
2026-11-11 22:36:13,933 - ERROR - [XLC] matureedesiree: The process exited with an error. Return code: 3199971767 (-1094995529)
[mpegts @ 00000187ff61d940] PES packet size mismatch
[mpegts @ 00000187ff61d940] Packet corrupt (stream = 1, dts = 202632630).
[in#0/hls @ 00000187fcec18c0] Packet corrupt (stream = 3, dts = 202632630).
[in#0/hls @ 00000187fcf4cb40] corrupt input packet in stream 3
[in#0/hls @ 00000187fcec18c0] Packet corrupt (stream = 3, dts = NOPTS).
[in#0/hls @ 00000187fcf4cb40] corrupt input packet in stream 3
[in#0/hls @ 00000187fcec18c0] Packet corrupt (stream = 3, dts = NOPTS).
[in#0/hls @ 00000187fcf4cb40] corrupt input packet in stream 3
[in#0/hls @ 00000187fcec18c0] Packet corrupt (stream = 3, dts = NOPTS).
[in#0/hls @ 00000187fcf4cb40] corrupt input packet in stream 3
[aac_adtstoasc @ 00000187ff2a5980] Error parsing ADTS frame header!
[mp4 @ 00000187ff836200] Error applying bitstream filters to an output packet for stream #1: Invalid data found when processing input
[aost#0:1/copy @ 0000018780700080] Error submitting a packet to the muxer: Invalid data found when processing input
    Last message repeated 1 times
[out#0/mp4 @ 00000187ffdcc140] Error muxing a packet
[out#0/mp4 @ 00000187ffdcc140] Task finished with error code: -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 00000187ffdcc140] Terminating thread with return code -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 00000187ffdcc140] video:68260KiB audio:3605KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: 0.129344%
frame= 4355 fps= 22 q=-1.0 Lsize=   71958KiB time=00:03:06.09 bitrate=3167.7kbits/s speed=0.956x elapsed=0:03:14.57    
Conversion failed!
2026-11-11 22:36:13,934 - INFO - [XLC] matureedesiree: Recording ended with error
2026-11-11 22:36:13,934 - INFO - [XLC] matureedesiree: Error on downloading
```

### kellydirty · 2026-11-11 22:42:06

```text
2026-11-11 22:42:06,617 - ERROR - [XLC] kellydirty: The process exited with an error. Return code: 3199971767 (-1094995529)
[tls @ 000001c655a27b00] Error in the pull function.
[tls @ 000001c655a27b00] IO error: Error number -10054 occurred
[https @ 000001c657131040] Opening 'https://spg1-eu-nl.wlresources.com/live-g3/eu-nl/3s/g257/segment/mp4:video_2085246_1094781950_high/58800816863966002.ts' for reading
[tls @ 000001c655a27b00] The specified session has been invalidated for some reason.
[in#0/hls @ 000001c6533016c0] keepalive request failed for 'https://spg1-eu-nl.wlresources.com/live-g3/eu-nl/3s/g257/segment/mp4:video_2085246_1094781950_high/58800816863966002.ts' with error: 'I/O error' when opening url, retrying with new connection
[in#0/hls @ 000001c6533016c0] Opening 'https://spg1-eu-nl.wlresources.com/live-g3/eu-nl/3s/g257/segment/mp4:video_2085246_1094781950_high/58800816863966002.ts' for reading
frame= 9680 fps= 23 q=-1.0 size=  134400KiB time=00:07:06.38 bitrate=2582.2kbits/s speed=1.01x elapsed=0:07:02.93    
frame= 9680 fps= 23 q=-1.0 size=  134400KiB time=00:07:06.38 bitrate=2582.2kbits/s speed=1.01x elapsed=0:07:03.45    
[mpegts @ 000001c6557ae700] PES packet size mismatch
[mpegts @ 000001c6557ae700] Packet corrupt (stream = 1, dts = 384558360).
[aac_adtstoasc @ 000001c6550147c0] Error parsing ADTS frame header!
[mp4 @ 000001c655ca1fc0] Error applying bitstream filters to an output packet for stream #1: Invalid data found when processing input
[aost#0:1/copy @ 000001c6560e51c0] Error submitting a packet to the muxer: Invalid data found when processing input
    Last message repeated 1 times
[out#0/mp4 @ 000001c6562274c0] Error muxing a packet
[out#0/mp4 @ 000001c6562274c0] Task finished with error code: -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 000001c6562274c0] Terminating thread with return code -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 000001c6562274c0] video:126669KiB audio:8041KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: 0.222605%
frame= 9682 fps= 23 q=-1.0 Lsize=  135010KiB time=00:07:08.21 bitrate=2582.8kbits/s speed=1.01x elapsed=0:07:03.51    
Conversion failed!
2026-11-11 22:42:06,618 - INFO - [XLC] kellydirty: Recording ended with error
2026-11-11 22:42:06,618 - INFO - [XLC] kellydirty: Error on downloading
```

### matureedesiree · 2026-11-11 22:48:48

```text
2026-11-11 22:48:48,853 - ERROR - [XLC] matureedesiree: The process exited with an error. Return code: 3199971767 (-1094995529)
[in#0/hls @ 00000244b65c3ec0] keepalive request failed for 'https://spg1-eu-nl.wlresources.com/live-g3/eu-nl/3s/g256/segment/mp4:video_2616773_1094783411_high/58800829704561436.ts' with error: 'I/O error' when opening url, retrying with new connection
[in#0/hls @ 00000244b65c3ec0] Opening 'https://spg1-eu-nl.wlresources.com/live-g3/eu-nl/3s/g256/segment/mp4:video_2616773_1094783411_high/58800829704561436.ts' for reading
frame=13188 fps= 18 q=-1.0 size=  217600KiB time=00:12:00.93 bitrate=2472.6kbits/s speed=0.991x elapsed=0:12:07.23    
frame=13188 fps= 18 q=-1.0 size=  217600KiB time=00:12:00.93 bitrate=2472.6kbits/s speed=0.991x elapsed=0:12:07.74    
[mpegts @ 00000244b85b3ac0] PES packet size mismatch
[mpegts @ 00000244b85b3ac0] Packet corrupt (stream = 1, dts = 270852150).
[in#0/hls @ 00000244b65c3ec0] Packet corrupt (stream = 3, dts = 270852150).
[in#0/hls @ 00000244b664a940] corrupt input packet in stream 3
[in#0/hls @ 00000244b65c3ec0] Packet corrupt (stream = 3, dts = NOPTS).
[in#0/hls @ 00000244b664a940] corrupt input packet in stream 3
[aac_adtstoasc @ 00000244b86be500] Error parsing ADTS frame header!
[mp4 @ 00000244b8cb6fc0] Error applying bitstream filters to an output packet for stream #1: Invalid data found when processing input
[aost#0:1/copy @ 00000244b9362980] Error submitting a packet to the muxer: Invalid data found when processing input
    Last message repeated 1 times
[out#0/mp4 @ 00000244b941a580] Error muxing a packet
[out#0/mp4 @ 00000244b941a580] Task finished with error code: -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 00000244b941a580] Terminating thread with return code -1094995529 (Invalid data found when processing input)
[out#0/mp4 @ 00000244b941a580] video:207116KiB audio:10937KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: 0.129029%
frame=13188 fps= 18 q=-1.0 Lsize=  218334KiB time=00:12:02.06 bitrate=2477.0kbits/s speed=0.992x elapsed=0:12:07.82    
Conversion failed!
2026-11-11 22:48:48,853 - INFO - [XLC] matureedesiree: Recording ended with error
2026-11-11 22:48:48,854 - INFO - [XLC] matureedesiree: Error on downloading
```

### kellydirty · 2026-11-12 14:48:08

```text
2026-11-12 14:48:08,245 - ERROR - [XLC] kellydirty: The process exited with an error. Return code: 3199971767 (-1094995529)
ffmpeg version 2026-05-06-git-f2e5eff3ff-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers
  built with gcc 15.2.0 (Rev13, Built by MSYS2 project)
  configuration: --enable-gpl --enable-version3 --enable-static --disable-w32threads --disable-autodetect --enable-cairo --enable-fontconfig --enable-iconv--enable-gnutls --enable-lcms2 --enable-libxml2 --enable-gmp --enable-bzlib --enable-lzma --enable-libsnappy --enable-zlib --enable-librist --enable-libsrt--enable-libssh --enable-libzmq --enable-avisynth --enable-libbluray --enable-libcaca --enable-libdvdnav --enable-libdvdread --enable-sdl2 --enable-libaribb24 --enable-libaribcaption --enable-libdav1d --enable-libdavs2 --enable-libopenjpeg --enable-libquirc --enable-libuavs3d --enable-libxevd --enable-libzvbi --enable-liboapv --enable-libqrencode --enable-librav1e --enable-libsvtav1 --enable-libvvenc --enable-libwebp --enable-libx264 --enable-libx265 --enable-libxavs2 --enable-libxeve --enable-libxvid --enable-libaom --enable-libjxl --enable-libsvtjpegxs --enable-libvpx --enable-mediafoundation --enable-libass --enable-frei0r --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-liblensfun --enable-libvidstab --enable-libvmaf --enable-libzimg --enable-amf --enable-cuda-llvm --enable-cuvid --enable-dxva2 --enable-d3d11va --enable-d3d12va --enable-ffnvcodec --enable-libvpl --enable-nvdec --enable-nvenc --enable-vaapi --enable-libshaderc --enable-vulkan --enable-libplacebo --enable-opencl --enable-libcdio --enable-openal --enable-libgme --enable-libmodplug --enable-libopenmpt --enable-libopencore-amrwb --enable-libmp3lame --enable-libshine --enable-libtheora --enable-libtwolame --enable-libvo-amrwbenc --enable-libcodec2 --enable-libilbc --enable-libgsm --enable-liblc3 --enable-libopencore-amrnb --enable-libopus --enable-libspeex --enable-libvorbis --enable-ladspa --enable-libbs2b --enable-libflite --enable-libmysofa --enable-librubberband --enable-libsoxr --enable-chromaprint --enable-whisper
  libavutil      60. 30.100 / 60. 30.100
  libavcodec     62. 30.100 / 62. 30.100
  libavformat    62. 15.100 / 62. 15.100
  libavdevice    62.  4.100 / 62.  4.100
  libavfilter    11. 17.100 / 11. 17.100
  libswscale      9.  7.100 /  9.  7.100
  libswresample   6.  4.100 /  6.  4.100
[in#0 @ 000001ad68d2cac0] Error opening input: Invalid data found when processing input
Error opening input file https://spg1-eu-nl.wlresources.com/live-g10/eu-nl/g2/3s/playlist/2085246/free/1094878859/playlist.m3u8?xpa=70a.
Error opening input files: Invalid data found when processing input
2026-11-12 14:48:08,246 - INFO - [XLC] kellydirty: Recording ended with error
2026-11-12 14:48:08,246 - INFO - [XLC] kellydirty: Error on downloading
```

