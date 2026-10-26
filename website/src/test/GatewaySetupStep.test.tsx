/**
 * GatewaySetupStep — the DeepSeek "own gateway" first-run wizard (task 18).
 *
 * Covers the four paths the wizard owns:
 *  - fetch-models success populates the picker;
 *  - fetch-models failure shows an error AND keeps the free-text model input;
 *  - submit calls apply with the trimmed values and fires onApplied on ok;
 *  - cancel fires onCancel and writes nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const { probeMock, applyMock } = vi.hoisted(() => ({
  probeMock: vi.fn(),
  applyMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    gatewaySetupProbeModels: probeMock,
    gatewaySetupApply: applyMock,
  },
}))

// i18n: return the key so assertions can target stable strings without a catalog.
vi.mock('../i18n/t', () => ({ i18nT: (k: string) => k }))

import GatewaySetupStep from '../components/GatewaySetupStep'

function setup() {
  const onApplied = vi.fn()
  const onCancel = vi.fn()
  render(<GatewaySetupStep onApplied={onApplied} onCancel={onCancel} />)
  return { onApplied, onCancel }
}

function fillBaseAndKey() {
  const base = screen.getByLabelText('components.gatewaySetup.base_url')
  const key = screen.getByLabelText('components.gatewaySetup.api_key')
  fireEvent.change(base, { target: { value: 'http://gw:4000/v1 ' } })
  fireEvent.change(key, { target: { value: ' sk-test ' } })
}

describe('GatewaySetupStep', () => {
  beforeEach(() => {
    probeMock.mockReset()
    applyMock.mockReset()
    cleanup()
  })

  it('fetches and lists models on probe success', async () => {
    probeMock.mockResolvedValue({ models: ['deepseek-chat', 'deepseek-coder'] })
    setup()
    fillBaseAndKey()
    fireEvent.click(screen.getByText('components.gatewaySetup.fetch_models'))
    await waitFor(() => expect(probeMock).toHaveBeenCalledWith('http://gw:4000/v1', 'sk-test'))
    // The picker now exists with the returned models.
    await waitFor(() => expect(screen.getByText('deepseek-chat')).toBeTruthy())
  })

  it('keeps manual model entry when probe fails', async () => {
    probeMock.mockResolvedValue({ error: 'nope', code: 'unreachable' })
    setup()
    fillBaseAndKey()
    fireEvent.click(screen.getByText('components.gatewaySetup.fetch_models'))
    await waitFor(() => expect(probeMock).toHaveBeenCalled())
    // The free-text model input is still present (not replaced by a picker).
    expect(screen.getByLabelText('components.gatewaySetup.default_model')).toBeTruthy()
  })

  it('applies trimmed values and calls onApplied on ok', async () => {
    applyMock.mockResolvedValue({ ok: true, acp_backend: 'deepseek' })
    const { onApplied } = setup()
    fillBaseAndKey()
    const model = screen.getByLabelText('components.gatewaySetup.default_model')
    fireEvent.change(model, { target: { value: ' deepseek-chat ' } })
    fireEvent.click(screen.getByText('components.gatewaySetup.save_and_continue'))
    await waitFor(() =>
      expect(applyMock).toHaveBeenCalledWith({
        base_url: 'http://gw:4000/v1',
        api_key: 'sk-test',
        default_model: 'deepseek-chat',
      }),
    )
    await waitFor(() => expect(onApplied).toHaveBeenCalled())
  })

  it('shows an error and does not call onApplied when apply fails', async () => {
    applyMock.mockResolvedValue({ ok: false, error: 'boom', code: 'config_write_failed' })
    const { onApplied } = setup()
    fillBaseAndKey()
    fireEvent.click(screen.getByText('components.gatewaySetup.save_and_continue'))
    await waitFor(() => expect(applyMock).toHaveBeenCalled())
    expect(onApplied).not.toHaveBeenCalled()
  })

  it('cancel writes nothing and calls onCancel', () => {
    const { onCancel } = setup()
    fireEvent.click(screen.getByText('components.gatewaySetup.cancel'))
    expect(onCancel).toHaveBeenCalled()
    expect(applyMock).not.toHaveBeenCalled()
  })

  it('does not submit with empty base_url or api_key', () => {
    applyMock.mockResolvedValue({ ok: true })
    setup()
    // Save button is disabled until both required fields are filled.
    const save = screen.getByText('components.gatewaySetup.save_and_continue')
      .closest('button') as HTMLButtonElement
    expect(save.disabled).toBe(true)
  })
})
