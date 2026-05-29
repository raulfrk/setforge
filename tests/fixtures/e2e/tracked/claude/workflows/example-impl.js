export const meta = {
  name: 'example-impl',
  description: 'E2E fixture workflow exercising install/compare/revert of the workflows deploy category.',
  phases: [{ title: 'Noop' }],
}

phase('Noop')
log('example workflow fixture — no agents dispatched')
