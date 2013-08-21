describe("go.conversation.models", function() {
  describe(".ConversationGroupsModel", function() {
    var models = go.conversation.models,
        ConversationGroupsModel = models.ConversationGroupsModel;

    var server,
        model;

    beforeEach(function() {
      server = sinon.fakeServer.create();

      model = new ConversationGroupsModel({
        key: 'conversation1',
        groups: [{
          key: 'group1',
          inConversation: true
        }, {
          key: 'group2',
          inConversation: true
        }, {
          key: 'group3',
          inConversation: true
        }]
      });
    });

    afterEach(function() {
      server.restore();
      go.testHelpers.unregisterModels();
    });

    describe(".save", function() {
      it("should only include the groups related to the conversation",
      function(done) {
        model
          .get('groups')
          .get('group3')
          .set('inConversation', false);

        server.respondWith(function(req) {
          var data = JSON.parse(req.requestBody);

          assert.deepEqual(
            data.groups,
            [{key: 'group1'}, {key: 'group2'}]);

          done();
        });

        model.save();
        server.respond();
      });

      it("should send a request to the server", function(done) {
        server.respondWith(function(req) {
          assert.equal(req.url, '/conversation/conversation1/edit_groups/');
          assert.deepEqual(JSON.parse(req.requestBody), {
            key: 'conversation1',
            groups: [
              {key: 'group1'},
              {key: 'group2'},
              {key: 'group3'}]
          });

          done();
        });

        model.save();
        server.respond();
      });
    });
  });
});