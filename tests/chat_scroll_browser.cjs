// Run with Playwright installed: node tests/chat_scroll_browser.cjs
const { chromium, webkit } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../chat_app/templates/index.html'), 'utf8');
(async () => {
  for (const engine of [chromium, webkit]) {
    const browser = await engine.launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
      await page.route('**/*', route => route.fulfill({ contentType: 'application/json', body: '{}' }));
      await page.setContent(html);
      await page.evaluate(() => {
        for (let i = 0; i < 30; i++) appendUserMessage('History line\n'.repeat(5));
        chatScroll.resume();
        window.bubble = createAssistantMessageBubble();
        window.stream = createStreamingMessage(bubble.contentDiv);
      });
      const bottomDistance = () => page.evaluate(() => messagesContainer.scrollHeight - messagesContainer.clientHeight - messagesContainer.scrollTop);
      for (let i = 0; i < 20; i++) {
        await page.evaluate(() => stream.append('## Heading\n**Text** and more text\n'.repeat(3)));
        assert.ok(Math.abs(await bottomDistance()) <= 1);
      }
      const box = await page.locator('#messagesContainer').boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, 500);
      await page.waitForTimeout(150);
      await page.evaluate(() => stream.append('Downward wheel at bottom\n'));
      assert.ok(Math.abs(await bottomDistance()) <= 1);
      await page.mouse.wheel(0, -250);
      await page.waitForTimeout(200);
      const paused = await page.evaluate(() => messagesContainer.scrollTop);
      await page.evaluate(() => stream.append('Streaming while reading history\n'.repeat(20)));
      await page.waitForTimeout(100);
      assert.equal(await page.evaluate(() => messagesContainer.scrollTop), paused);
      await page.mouse.wheel(0, 10000);
      await page.waitForTimeout(250);
      await page.evaluate(() => stream.append('Following again\n'));
      assert.ok(Math.abs(await bottomDistance()) <= 1);
      await page.evaluate(() => stream.finish());
      await page.waitForTimeout(100);
      assert.ok(Math.abs(await bottomDistance()) <= 1);
      assert.ok(await page.locator('.content .md-h2').count() > 0);
      await page.evaluate(async () => {
        const payload = 'data: {"token":"**Grüße**"}\n\ndata: {"done":true}\n\n';
        const bytes = new TextEncoder().encode(payload);
        window.fetch = async () => new Response(new ReadableStream({
          start(controller) {
            for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
            controller.close();
          }
        }));
        messageInput.value = 'Streaming integration';
        await sendMessage();
      });
      assert.equal(await page.locator('.content').last().locator('strong').textContent(), 'Grüße');
      await page.evaluate(async () => {
        window.fetch = async () => new Response('data: {"done":true}\n\n');
        messageInput.value = 'Empty response';
        await sendMessage();
      });
      assert.equal(await page.locator('.content').last().textContent(), 'Keine Antwort erhalten.');
      // Exercise the actual sendMessage path, including an interrupted request.
      await page.evaluate(async () => {
        window.fetch = async () => { throw new Error('offline'); };
        messageInput.value = 'Test';
        await sendMessage();
      });
      assert.match(await page.locator('.content').last().textContent(), /Fehler bei der Kommunikation: offline/);
      console.log(`${engine.name()}: streaming, wheel pause/resume, completion and errors passed`);
    } finally {
      await browser.close();
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
