#!/usr/bin/env node

const { spawn } = require('child_process')

const remoteUrl =
	process.env.MCP_REMOTE_URL || 'https://beta.collegue.dev/mcp/'

const passthroughArgs = process.argv.slice(2)

const autoHeadersArgs = []

if (process.env.GITHUB_TOKEN) {
	autoHeadersArgs.push('--header', `X-GitHub-Token:${process.env.GITHUB_TOKEN}`)
}

if (process.env.SENTRY_AUTH_TOKEN) {
	autoHeadersArgs.push(
		'--header',
		`X-Sentry-Token:${process.env.SENTRY_AUTH_TOKEN}`
	)
}

if (process.env.SENTRY_ORG) {
	autoHeadersArgs.push('--header', `X-Sentry-Org:${process.env.SENTRY_ORG}`)
}

if (process.env.SENTRY_URL) {
	autoHeadersArgs.push('--header', `X-Sentry-Url:${process.env.SENTRY_URL}`)
}

const args = [
	'-y',
	'mcp-remote',
	remoteUrl,
	'--transport',
	'http-only',
	...autoHeadersArgs,
	...passthroughArgs,
]

const child = spawn('npx', args, { stdio: 'inherit' })

child.on('error', (err) => {
	console.error(`Error executing mcp-remote: ${err.message}`)
	process.exit(1)
})

child.on('close', (code) => {
	process.exit(code ?? 1)
})
