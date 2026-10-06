// Runs inside the isolated Ghost fixture. All credentials are disposable and local.
const fs = require('fs');
const jwt = require('/var/lib/ghost/current/node_modules/jsonwebtoken');
const key = JSON.parse(fs.readFileSync('/tmp/fixture-key.json')).api_keys[0];
const receiver = process.argv[2];
const secret = 'native-fixture-signing-secret-32-bytes';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function api(path, method, body) {
    const token = jwt.sign({}, Buffer.from(key.secret, 'hex'), {
        keyid: key.id, algorithm: 'HS256', expiresIn: '5m', audience: '/admin/'
    });
    const res = await fetch('http://127.0.0.1:2368/ghost/api/admin/' + path, {
        method, headers: {Authorization: 'Ghost ' + token, 'Content-Type': 'application/json',
            'X-Forwarded-Proto': 'https'}, body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error('Fixture API ' + path + ': ' + res.status + ' ' + await res.text());
    return res.json();
}
(async () => {
    if (process.argv[3] === 'failures') {
        for (const mode of ['unavailable', 'timeout', 'gone']) {
            await api('webhooks/', 'POST', {webhooks: [{name: mode, event: 'post.published',
                target_url: new URL('/fixture/' + mode, receiver).href, secret}]});
        }
        await api('posts/', 'POST', {posts: [{title: 'Failure fixture first', status: 'published'}]});
        await sleep(8000);
        const models = require('/var/lib/ghost/current/core/server/models');
        async function receipts() {
            const hooks = await models.Webhook.findAll({context: {internal: true}});
            return hooks.toJSON().filter(h => ['unavailable', 'timeout', 'gone'].includes(h.name))
                .map(h => ({name: h.name, status: h.last_triggered_status, error: h.last_triggered_error}));
        }
        const first = await receipts();
        await api('posts/', 'POST', {posts: [{title: 'Failure fixture second', status: 'published'}]});
        await sleep(8000);
        console.log(JSON.stringify({scenario: 'failures', first, second: await receipts()}));
        process.exit(0);
    }
    await api('webhooks/', 'POST', {webhooks: [{name: 'Native signed publish',
        event: 'post.published', target_url: receiver, secret}]});
    if (process.argv[3] === 'routes') {
        let post = (await api('posts/', 'POST', {posts: [{title: 'Custom route fixture',
            slug: 'native-route', tags: [{name: 'News', slug: 'news'}],
            canonical_url: 'https://external.example.test/original/', status: 'draft'}]})).posts[0];
        const versions = [];
        async function edit(values) {
            post = (await api('posts/' + post.id + '/', 'PUT',
                {posts: [{updated_at: post.updated_at, ...values}]})).posts[0];
            await sleep(1300);
        }
        await edit({status: 'published'});
        versions.push({url: post.url, canonical_url: post.canonical_url});
        await edit({tags: [{name: 'Archive', slug: 'archive'}], slug: 'moved-route'});
        await edit({status: 'draft'});
        await edit({status: 'published'});
        versions.push({url: post.url, canonical_url: post.canonical_url});
        console.log(JSON.stringify({scenario: 'custom-collections-slug-change-external-canonical',
            post_id: post.id, versions}));
        return;
    }
    let post = (await api('posts/', 'POST', {posts: [{title: 'Native publication fixture', status: 'draft'}]})).posts[0];
    async function edit(values) {
        post = (await api('posts/' + post.id + '/', 'PUT', {posts: [{updated_at: post.updated_at, ...values}]})).posts[0];
        await sleep(1300);
    }
    await edit({status: 'published'});
    await edit({title: 'Native edited fixture'});
    await edit({status: 'draft'});
    await edit({status: 'published'});
    console.log(JSON.stringify({scenario: 'publish-edit-unpublish-republish', post_id: post.id}));
})().catch(err => {console.error(err.message); process.exit(1);});
