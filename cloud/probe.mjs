import { createWorkBuddyCloud } from '@tencent-ai/workbuddy-cloud-sdk'
import publicConfig from './public-config.json' with { type: 'json' }

async function main() {
  const cloud = createWorkBuddyCloud({
    endpoint: publicConfig.endpoint,
    oauthRelayBaseUrl: publicConfig.oauthRelayBaseUrl,
    publishableKey: publicConfig.publishableKey,
  })
  const models = await cloud.llm.models.list()
  const available = models.filter((item) => item.enabled !== false && item.disabled !== true)
  console.log(JSON.stringify({ stage: 'models', count: available.length }))
  const model = available.find((item) => item.id === 'hy4free') || available.find((item) => item.id === 'hy3') || available[0]
  if (!model) throw new Error('模型目录为空')
  console.log(JSON.stringify({ stage: 'selected', id: model.id, name: model.name }))
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 120000)
  let answer = ''
  let usage = null
  try {
    for await (const chunk of cloud.llm.chat.completions.create({
      model: model.id,
      messages: [
        { role: 'system', content: '你是严谨的接口测试助手，只输出 JSON。' },
        { role: 'user', content: '只输出 {"ok":true}，不要添加其他内容。' },
      ],
      stream: true,
      stream_options: { include_usage: true },
      response_format: { type: 'json_object' },
      signal: controller.signal,
    })) {
      const choice = chunk.choices?.[0]
      if (choice?.delta?.content) answer += choice.delta.content
      if (chunk.usage) usage = chunk.usage
    }
  } finally {
    clearTimeout(timer)
  }
  console.log(JSON.stringify({ stage: 'answer', answer, usage }))
}

main().catch((error) => {
  console.error(JSON.stringify({
    stage: 'error',
    message: error?.message || String(error),
    code: error?.error?.code || error?.code || null,
    status: error?.status || null,
    requestId: error?.requestId || null,
  }))
  process.exitCode = 1
})
