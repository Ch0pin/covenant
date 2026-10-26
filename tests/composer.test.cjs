const {test}=require('node:test');
const assert=require('node:assert/strict');
const composer=require('../covenant/static/composer.js');
const markdown=require('../covenant/static/markdown.js');
const handles=['sol','reviewer','operator','everyone'];
test('complete leading mentions become unique chips while partial and inline mentions remain editable',()=>{
  const result=composer.extractRecipients('@Sol @reviewer @sol Please check',handles);
  assert.deepEqual(result.recipients,['sol','reviewer']);assert.equal(result.body,'Please check');
  for(const value of ['@so','@sol','Hello @sol ','@unknown hello']){
    const result=composer.extractRecipients(value,handles);assert.equal(result.body,value);assert.deepEqual(result.recipients,[]);
  }
});
test('recipient extraction and serialization preserve Markdown indentation and route all selected recipients',()=>{
  const text='@sol @reviewer\n    const value = 1;\n    return value;';
  const result=composer.extractRecipients(text,handles);
  assert.equal(result.body,'    const value = 1;\n    return value;');
  const wire=composer.serialize(result.recipients,result.body);
  assert.deepEqual(markdown.splitRecipients(wire),{recipients:['sol','reviewer'],body:result.body});
});
test('removing recipients changes only routing and never discards message text',()=>{
  const body='Please review **this** and ask @operator if blocked.';
  assert.equal(composer.serialize(['sol','reviewer'],body),'@sol @reviewer\n'+body);
  assert.equal(composer.serialize(['reviewer'],body),'@reviewer\n'+body);
  assert.equal(composer.serialize([],body),body);
});
