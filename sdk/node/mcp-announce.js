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
 * @param {number} [options.port=9099] - The HTTP port where the MCP server is listening.
 * @param {number} [options.listenPort=9099] - UDP port to listen on for discovery broadcasts.
 * @returns {dgram.Socket} The socket (call socket.close() to stop).
 */
function createDiscoveryResponder({ name, description, tools, port = 9099, listenPort = 9099 }) {
  const manifest = JSON.stringify({
    type: 'announce',
    name,
    description,
    tools,
    port,
  });

  const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });

  socket.on('message', (data, rinfo) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === 'discovery') {
        console.log(`Discovery request from ${rinfo.address}:${rinfo.port}, announcing`);
        socket.send(manifest, rinfo.port, rinfo.address);
      }
    } catch {
      // ignore malformed messages
    }
  });

  socket.on('error', (err) => {
    console.error('Announce socket error:', err.message);
  });

  socket.bind(listenPort, '0.0.0.0', () => {
    console.log(`Discovery responder listening on UDP :${listenPort} for ${name}`);
  });

  return socket;
}

module.exports = { createDiscoveryResponder };
