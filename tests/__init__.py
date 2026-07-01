import io, logging, sys

logging.basicConfig()
if '-v' in sys.argv:
  logging.getLogger().setLevel(logging.DEBUG)
else:
  # don't emit logs
  handler = logging.getLogger().handlers[0]
  if hasattr(handler, 'setStream'):
    handler.setStream(io.StringIO())

