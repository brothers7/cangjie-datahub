/** 指标合并弹窗：把源指标并入目标指标（源废弃）。 */
import { useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Field, Modal } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { Metric } from '@/types'

export function MergeModal({ from, metrics, onClose, onDone }: {
  from: Metric
  metrics: Metric[]
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const candidates = metrics.filter((m) => m.id !== from.id && m.status === 'active')

  const doMerge = async () => {
    if (!target) return
    setBusy(true)
    try {
      await api.mergeMetric(from.id, target)
      toast.ok(t('kg.merged'))
      onDone()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={t('kg.mergeTitle')} width={480} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={!target || busy} onClick={() => void doMerge()}>{t('kg.confirmMerge')}</button>
      </>}>
      <div className="col" style={{ gap: 12 }}>
        <div className="tiny dim">
          {t('kg.mergePre')}<b>{from.name}</b>{t('kg.mergeMid')}<b>{t('kg.mergeBold')}</b>{t('kg.mergePost')}
        </div>
        <Field label={t('kg.targetMetric')}>
          <select className="select" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{t('kg.selectPlaceholder')}</option>
            {candidates.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
      </div>
    </Modal>
  )
}
