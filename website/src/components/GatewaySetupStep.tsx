/**
 * First-run gateway setup wizard for the DeepSeek Harness "own gateway" path.
 *
 * Opened from `KiroPrerequisiteGate`'s backend selector when the user picks the
 * `deepseek` backend and no gateway is configured yet. Collects base_url,
 * api_key and an optional default model, probes the gateway's /models list to
 * populate a picker (degrading to free text on failure), then calls
 * `gatewaySetupApply`, which writes the vault key + config and flips
 * `acp_backend` to deepseek. On success the parent re-reads prerequisite status
 * (the foreign early-return then reports ready and admits the dashboard).
 */
import { useState } from 'react'
import { api } from '../api/client'
import { Btn, Card, SendBtn } from './ui'
import ErrorNotice from './ErrorNotice'
import SimpleSelect from './SimpleSelect'
import { i18nT } from '../i18n/t'

export default function GatewaySetupStep({
  onApplied,
  onCancel,
}: {
  onApplied: () => void
  onCancel: () => void
}) {
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState('')
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState('')

  const canSubmit = baseUrl.trim() !== '' && apiKey.trim() !== '' && !applying

  const probe = async () => {
    setProbeError('')
    if (!baseUrl.trim() || !apiKey.trim()) {
      setProbeError(i18nT('components.gatewaySetup.base_url_and_key_required'))
      return
    }
    setProbing(true)
    try {
      const res = await api.gatewaySetupProbeModels(baseUrl.trim(), apiKey.trim())
      if (res.models && res.models.length > 0) {
        setModels(res.models)
        if (!model) setModel(res.models[0])
      } else {
        setProbeError(res.error || i18nT('components.gatewaySetup.no_models_returned'))
      }
    } catch {
      setProbeError(i18nT('components.gatewaySetup.could_not_reach_gateway'))
    } finally {
      setProbing(false)
    }
  }

  const submit = async () => {
    setApplyError('')
    if (!canSubmit) return
    setApplying(true)
    try {
      const res = await api.gatewaySetupApply({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        default_model: model.trim() || undefined,
      })
      if (res.ok) {
        onApplied()
      } else {
        setApplyError(res.error || i18nT('components.gatewaySetup.could_not_save'))
      }
    } catch {
      setApplyError(i18nT('components.gatewaySetup.could_not_save'))
    } finally {
      setApplying(false)
    }
  }

  return (
    <Card className="border-accent/60">
      <h2 className="text-base font-semibold text-text-strong">
        {i18nT('components.gatewaySetup.configure_your_model_gateway')}
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">
        {i18nT('components.gatewaySetup.intro')}
      </p>

      <label className="mt-5 block text-[13px] font-medium text-text">
        {i18nT('components.gatewaySetup.base_url')}
        <input
          type="url"
          className="mt-1 w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text focus-ring"
          placeholder="http://10.3.20.46:4000/v1"
          value={baseUrl}
          onChange={e => setBaseUrl(e.target.value)}
          autoComplete="off"
          aria-label={i18nT('components.gatewaySetup.base_url')}
        />
      </label>

      <label className="mt-4 block text-[13px] font-medium text-text">
        {i18nT('components.gatewaySetup.api_key')}
        <input
          type="password"
          className="mt-1 w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text focus-ring"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          autoComplete="off"
          aria-label={i18nT('components.gatewaySetup.api_key')}
        />
      </label>

      <div className="mt-4">
        <Btn type="button" disabled={probing} onClick={probe}>
          {probing
            ? i18nT('components.gatewaySetup.fetching_models')
            : i18nT('components.gatewaySetup.fetch_models')}
        </Btn>
      </div>
      {probeError && <ErrorNotice className="mt-3 text-xs" message={probeError} />}

      <label className="mt-4 block text-[13px] font-medium text-text">
        {i18nT('components.gatewaySetup.default_model')}
        {models.length > 0 ? (
          <div className="mt-1">
            <SimpleSelect
              options={models}
              value={model}
              onChange={setModel}
              aria-label={i18nT('components.gatewaySetup.default_model')}
            />
          </div>
        ) : (
          <input
            type="text"
            className="mt-1 w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text focus-ring"
            placeholder={i18nT('components.gatewaySetup.model_placeholder')}
            value={model}
            onChange={e => setModel(e.target.value)}
            autoComplete="off"
            aria-label={i18nT('components.gatewaySetup.default_model')}
          />
        )}
      </label>

      {applyError && <ErrorNotice className="mt-3 text-xs" message={applyError} />}

      <div className="mt-5 flex items-center gap-2">
        <SendBtn type="button" disabled={!canSubmit} onClick={submit}>
          {applying
            ? i18nT('components.gatewaySetup.saving')
            : i18nT('components.gatewaySetup.save_and_continue')}
        </SendBtn>
        <Btn type="button" disabled={applying} onClick={onCancel}>
          {i18nT('components.gatewaySetup.cancel')}
        </Btn>
      </div>
    </Card>
  )
}
