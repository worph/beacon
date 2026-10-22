/**
 * UDP discovery responder — lets MCP servers announce themselves to the aggregator.
 * Zero dependencies, uses built-in Node.js dgram module.
 */

const dgram = require('dgram');

// Ack mode: when a discovery request carries `ack`, reply with 8 bytes —
// 'BCNA' + the FNV-1a 32-bit hash of the manifest, big-endian — and send the
// manifest itself only when Beacon asks for it 1:1 with {"type":"manifest"}.
// Full manifests answered to a broadcast all land on Beacon at once and
// overflow its socket buffer; an ack never can. Requests without `ack` (an
// older Beacon) still get the full manifest.
const ACK_MAGIC = Buffer.from('BCNA');

function fnv1a32(buf) {
  let h = 0x811c9dc5;
  for (const b of buf) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return h;
}

function ackFor(manifest) {
  const ack = Buffer.alloc(8);
  ACK_MAGIC.copy(ack, 0);
  ack.writeUInt32BE(fnv1a32(manifest), 4);
  return ack;
}

/**
 * Start a UDP listener that responds to discovery broadcasts.
 *
 * @param {Object} options
 * @param {string} options.name - Server name for identification.
 * @param {string} options.description - Human-readable description.
 * @param {Array} options.tools - List of tool definitions (name, description, inputSchema).
 *   Each tool object may include an optional `direct: true` flag to have Beacon expose
 *   the tool as a first-class MCP tool alongside its meta-tools, instead of only through `beacon_call`.
 * @param {number} [options.port=9099] - The HTTP port where the MCP server is listening.
 * @param {string} [options.path] - HTTP path for the MCP endpoint (default: /mcp). Set if your server uses a non-standard path like /api/mcp.
 * @param {number} [options.listenPort=9099] - UDP port to listen on for discovery broadcasts.
 * @param {Object} [options.auth] - Optional auth descriptor, e.g. { type: 'bearer', token: 'secret' }. Passed to the aggregator so it can authenticate when calling tools.
 * @param {Function} [options.onDiscovery] - Optional callback invoked when a discovery message is received. Called with { mcp_url } if the broadcast includes it.
 * @returns {dgram.Socket} The socket (call socket.close() to stop).
 */
function createDiscoveryResponder({ name, description, tools, port = 9099, path, listenPort = 9099, auth, onDiscovery }) {
  const payload = { type: 'announce', name, description, tools, port };
  if (path) payload.path = path;
  if (auth) payload.auth = auth;
  const manifest = Buffer.from(JSON.stringify(payload));
  const ack = ackFor(manifest);

  const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });

  socket.on('message', (data, rinfo) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === 'manifest') {
        socket.send(manifest, rinfo.port, rinfo.address);
      } else if (msg.type === 'discovery') {
        console.log(`Discovery request from ${rinfo.address}:${rinfo.port}, ${msg.ack ? 'acking' : 'announcing'}`);
        socket.send(msg.ack ? ack : manifest, rinfo.port, rinfo.address);
        if (onDiscovery) {
          onDiscovery({ mcp_url: msg.mcp_url || null });
        }
      }
    } catch {
      // ignore malformed messages
    }
  });

  socket.on('error', (err) => {
    console.error('Announce socket error:', err.message);
  });

  socket.bind(listenPort, '0.0.0.0', () => {
    // Join multicast group so we receive discovery packets on networks
    // where broadcast doesn't work (e.g. plain `docker network create`)
    socket.addMembership('239.255.99.1');
    console.log(`Discovery responder listening on UDP :${listenPort} (multicast 239.255.99.1) for ${name}`);
  });

  return socket;
}

module.exports = { createDiscoveryResponder };
