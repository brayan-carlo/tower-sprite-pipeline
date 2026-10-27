function invalid() {
  const error = new Error("MP4_METADATA_INVALID");
  error.code = "MP4_METADATA_INVALID";
  return error;
}

function uint64(bytes, offset) {
  const value = bytes.readBigUInt64BE(offset);
  if (value > BigInt(Number.MAX_SAFE_INTEGER)) throw invalid();
  return Number(value);
}

function boxes(bytes, start = 0, end = bytes.length) {
  if (!Buffer.isBuffer(bytes) || start < 0 || end > bytes.length || start > end) throw invalid();
  const result = [];
  let offset = start;
  while (offset < end) {
    if (end - offset < 8) throw invalid();
    const smallSize = bytes.readUInt32BE(offset);
    const type = bytes.toString("ascii", offset + 4, offset + 8);
    if (!/^[\x20-\x7e]{4}$/.test(type)) throw invalid();
    let headerSize = 8;
    let size = smallSize;
    if (smallSize === 1) {
      if (end - offset < 16) throw invalid();
      size = uint64(bytes, offset + 8);
      headerSize = 16;
    } else if (smallSize === 0) {
      size = end - offset;
    }
    if (!Number.isSafeInteger(size) || size < headerSize || offset + size > end) throw invalid();
    result.push({ type, start: offset + headerSize, end: offset + size });
    offset += size;
  }
  if (offset !== end) throw invalid();
  return result;
}

function only(items, type) {
  const matches = items.filter((item) => item.type === type);
  if (matches.length !== 1) throw invalid();
  return matches[0];
}

function movieDuration(bytes, mvhd) {
  const length = mvhd.end - mvhd.start;
  const version = bytes[mvhd.start];
  let timescale;
  let duration;
  if (version === 0) {
    if (length !== 100) throw invalid();
    timescale = bytes.readUInt32BE(mvhd.start + 12);
    duration = bytes.readUInt32BE(mvhd.start + 16);
  } else if (version === 1) {
    if (length !== 112) throw invalid();
    timescale = bytes.readUInt32BE(mvhd.start + 20);
    duration = uint64(bytes, mvhd.start + 24);
  } else {
    throw invalid();
  }
  const seconds = duration / timescale;
  if (timescale === 0 || duration === 0 || !Number.isFinite(seconds) || seconds <= 0) throw invalid();
  return seconds;
}

function handlerType(bytes, hdlr) {
  if (hdlr.end - hdlr.start < 12 || bytes[hdlr.start] !== 0) throw invalid();
  return bytes.toString("ascii", hdlr.start + 8, hdlr.start + 12);
}

function trackDimensions(bytes, tkhd) {
  const length = tkhd.end - tkhd.start;
  const version = bytes[tkhd.start];
  const exactLength = version === 0 ? 84 : version === 1 ? 96 : 0;
  const dimensionOffset = version === 0 ? 76 : 88;
  if (exactLength === 0 || length !== exactLength) throw invalid();
  const widthFixed = bytes.readUInt32BE(tkhd.start + dimensionOffset);
  const heightFixed = bytes.readUInt32BE(tkhd.start + dimensionOffset + 4);
  const width = widthFixed / 65536;
  const height = heightFixed / 65536;
  if (
    !Number.isInteger(width)
    || !Number.isInteger(height)
    || width <= 0
    || height <= 0
  ) throw invalid();
  return { width, height };
}

export function readMp4Metadata(bytes) {
  if (!(bytes instanceof Uint8Array)) throw invalid();
  const source = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (source.length < 24 || source.length > 100 * 1024 * 1024) throw invalid();
  const top = boxes(source);
  only(top, "ftyp");
  const moov = only(top, "moov");
  const movieBoxes = boxes(source, moov.start, moov.end);
  const duration_seconds = movieDuration(source, only(movieBoxes, "mvhd"));
  const videoTracks = [];
  for (const trak of movieBoxes.filter((item) => item.type === "trak")) {
    const trackBoxes = boxes(source, trak.start, trak.end);
    const mdiaMatches = trackBoxes.filter((item) => item.type === "mdia");
    if (mdiaMatches.length !== 1) throw invalid();
    const mediaBoxes = boxes(source, mdiaMatches[0].start, mdiaMatches[0].end);
    if (handlerType(source, only(mediaBoxes, "hdlr")) === "vide") {
      videoTracks.push(trackDimensions(source, only(trackBoxes, "tkhd")));
    }
  }
  if (videoTracks.length !== 1) throw invalid();
  return { duration_seconds, ...videoTracks[0] };
}
