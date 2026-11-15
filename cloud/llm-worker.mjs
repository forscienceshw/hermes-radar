import { createWorkBuddyCloud } from '@tencent-ai/workbuddy-cloud-sdk'
import publicConfig from './public-config.json' with { type: 'json' }

function cloudClient() {
  return createWorkBuddyCloud({
    endpoint: publicConfig.endpoint,
    oauthRelayBaseUrl: publicConfig.oauthRelayBaseUrl,
    publishableKey: publicConfig.publishableKey,
  })
}

function emit(event) {
  // Progress lines are newline-delimited JSON on stderr; the Python side reads
  // them while the request is running and turns them into a live progress bar.
  process.stderr.write(JSON.stringify({ event, at: Date.now() }) + '\n')
}

async function readRequest() {
  let input = ''
  for await (const chunk of process.stdin) input += chunk
  if (input.length > 30_000_000) throw new Error('请求超过 30 MB 限制')
  return JSON.parse(input)
}

function availableModels(models) {
  return models.filter((item) => item.enabled !== false && item.disabled !== true)
}

async function main() {
  const request = await readRequest()
  const cloud = cloudClient()
  const models = availableModels(await cloud.llm.models.list())
  if (request.mode === 'models') {
    const safe = models.map((item) => ({
      id: item.id, name: item.name ?? item.id, enabled: item.enabled,
      disabled: item.disabled, credits: item.credits,
      supportsImages: item.supportsImages, supportsReasoning: item.supportsReasoning,
      maxInputTokens: item.maxInputTokens, maxOutputTokens: item.maxOutputTokens,
    }))
    process.stdout.write(JSON.stringify({ ok: true, models: safe }))
    return
  }
  const selected = models.find((item) => item.id === request.model)
  if (!selected) throw new Error(`模型 ${request.model} 不在当前应用可用目录中`)
  const messages = request.messages
  if (!Array.isArray(messages) || messages[0]?.role !== 'system') {
    throw new Error('messages 必须以应用自己的 system 消息开始')
  }
  let content = ''
  let reasoning = ''
  let usage = null
  let lastEmit = 0
  let chunks = 0
  const started = Date.now()
  emit('start')
  for await (const chunk of cloud.llm.chat.completions.create({
    model: selected.id,
    messages,
    stream: true,
    stream_options: { include_usage: true },
    temperature: request.temperature ?? 0.2,
    response_format: request.response_format ?? { type: 'json_object' },
  })) {
    const delta = chunk.choices?.[0]?.delta
    if (delta?.content) content += delta.content
    if (delta?.reasoning_content) reasoning += delta.reasoning_content
    if (chunk.usage) usage = chunk.usage
    chunks += 1
    const now = Date.now()
    if (now - lastEmit >= 1500) {
      lastEmit = now
      process.stderr.write(JSON.stringify({
        event: 'progress', contentChars: content.length, reasoningChars: reasoning.length,
        chunks, elapsedMs: now - started,
      }) + '\n')
    }
  }
  emit('done')
  process.stdout.write(JSON.stringify({
    ok: true, content, reasoning, usage, model: selected.id,
    elapsedMs: Date.now() - started,
  }))
}

main().catch((error) => {
  process.stderr.write(JSON.stringify({
    ok: false,
    message: error?.message || String(error),
    code: error?.error?.code || error?.code || null,
    status: error?.status || null,
    requestId: error?.requestId || null,
    retryAfterMs: error?.retryAfterMs || null,
  }))
  process.exitCode = 1
})
