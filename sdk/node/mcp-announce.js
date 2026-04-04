/**
 * UDP discovery responder — lets MCP servers announce themselves to the aggregator.
 * Zero dependencies, uses built-in Node.js dgram module.
 */

const dgram = require('dgram');

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
  const manifest = JSON.stringify(payload);

  const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });

  socket.on('message', (data, rinfo) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === 'discovery') {
        console.log(`Discovery request from ${rinfo.address}:${rinfo.port}, announcing`);
        socket.send(manifest, rinfo.port, rinfo.address);
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
