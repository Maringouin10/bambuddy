/**
 * PrintModal with a Creality (Klipper / Moonraker) printer.
 *
 * Reported on a real setup: a file sliced for "Creality Ender-3 V3" showed
 * "File was sliced for Creality Ender-3 V3, but printing on P1S", and the Ender
 * was not offered at all. Creality printers are valid targets for any Creality
 * G-code (never for Bambu files), and the Bambu-only print options do not apply.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintModal } from '../../components/PrintModal';

const PRINTERS = [
  { id: 1, name: 'My P1S', model: 'P1S', printer_type: 'bambu', ip_address: '10.0.0.1', is_active: true, enabled: true },
  {
    id: 2,
    name: 'My Ender',
    model: 'Ender-3 V3 Plus',
    printer_type: 'creality',
    ip_address: '10.0.0.2',
    is_active: true,
    enabled: true,
  },
];

function mockBackend(slicedFor: string) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [], temperatures: {} }),
    ),
    http.get('/api/v1/library/files/:id', ({ params }) =>
      HttpResponse.json({
        id: Number(params.id),
        filename: 'Benchy.gcode',
        file_type: 'gcode',
        sliced_for_model: slicedFor,
      }),
    ),
    http.get('/api/v1/library/files/:id/plates', ({ params }) =>
      HttpResponse.json({ file_id: Number(params.id), filename: 'Benchy.gcode', plates: [], is_multi_plate: false }),
    ),
    http.get('/api/v1/library/files/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
  );
}

describe('PrintModal for Creality printers', () => {
  beforeEach(() => mockBackend('Creality Ender-3 V3'));

  it('offers the Ender as a print target for a file sliced for a neighbouring Creality profile', async () => {
    render(<PrintModal mode="create" libraryFileId={1} archiveName="Benchy" onClose={() => {}} />);

    expect(await screen.findByText('My Ender')).toBeInTheDocument();
    // The P1S is not a match for Creality G-code and is not offered by default.
    expect(screen.queryByText('My P1S')).not.toBeInTheDocument();
  });

  it('does not warn that the file was sliced for a different model when printing on the Ender', async () => {
    render(
      <PrintModal
        mode="create"
        libraryFileId={1}
        archiveName="Benchy"
        initialSelectedPrinterIds={[2]}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: /^print$/i })).toBeInTheDocument());
    // let the file details (sliced_for_model) load before asserting that nothing was flagged
    await screen.findByText(/print job/i);
    await waitFor(() => expect(screen.queryByText(/but printing on/i)).not.toBeInTheDocument());
  });

  it('hides the Bambu-only print options when the target is a Creality printer', async () => {
    render(
      <PrintModal
        mode="create"
        libraryFileId={1}
        archiveName="Benchy"
        initialSelectedPrinterIds={[2]}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: /^print$/i })).toBeInTheDocument());
    await screen.findByText(/print job/i);
    expect(screen.queryByText(/bed levelling/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/flow calibration/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/preheat & heat soak/i)).not.toBeInTheDocument();
  });

  it('still shows those options (and the usual model warning) for a Bambu target', async () => {
    mockBackend('X1C');
    render(
      <PrintModal
        mode="create"
        libraryFileId={1}
        archiveName="Benchy"
        initialSelectedPrinterIds={[1]}
        onClose={() => {}}
      />,
    );

    await screen.findByText(/print job/i);
    expect(await screen.findByText(/bed levelling/i)).toBeInTheDocument();
    expect(await screen.findByText(/but printing on P1S/i)).toBeInTheDocument();
  });
});
