import persistent
import threading
import time
import transaction

from transaction.interfaces import ISavepointDataManager

from zope.interface import implementer

from ZODB.POSException import ConflictError

from pyramid.threadlocal import get_current_registry

from substanced.interfaces import (
    IIndexingActionProcessor,
    MODE_DEFERRED,
    )

class Action(object):
    def __repr__(self):
        klass = self.__class__
        classname = '%s.%s' % (klass.__module__, klass.__name__)
        return '<%s object docid %r for index %r at %#x>' % (
            classname,
            self.docid,
            getattr(self.index, '__name__', None),
            id(self)
            )

class ActionsQueue(persistent.Persistent):
    def __init__(self):
        self.actions = []

    def extend(self, actions):
        self.actions.extend(actions)
        self._p_changed = True

    def popall(self):
        if not self.actions:
            return None
        actions = self.actions[:]
        self.actions = []
        return actions

    def _p_resolveConflict(self, old_state, committed_state, new_state):
        for k, v in new_state.items():
            if k != 'actions':
                if committed_state.get(k) != v:
                    # cant cope with a conflict for anything except 'actions'
                    raise ConflictError

        committed_actions = committed_state['actions']
        new_actions = new_state['actions']
        all_actions = committed_actions + new_actions
        print 'resolved actionsqueue conflict with result %r' % (all_actions,)
        committed_state['actions'] = all_actions
        return committed_state

class DumberNDirtActionProcessor(object):
    def __init__(self, context):
        self.context = context

    def get_root(self):
        jar = self.context._p_jar
        if jar is None:
            return None
        zodb_root = jar.root()
        return zodb_root

    def get_queue(self):
        zodb_root = self.get_root()
        if zodb_root is None:
            return None
        queue = zodb_root.get('dndqueue')
        return queue

    def active(self):
        return self.get_queue() is not None

    def engage(self):
        self.sync()
        queue = self.get_queue()
        if queue is None:
            zodb_root = self.get_root()
            if zodb_root is None:
                raise RuntimeError('Context has no jar')
            queue = ActionsQueue()
            zodb_root['dndqueue'] = queue
            transaction.commit()

    def disengage(self):
        self.sync()
        zodb_root = self.get_root()
        if zodb_root is None:
            raise RuntimeError('Context has no jar')
        zodb_root.pop('dndqueue', None)
        transaction.commit()

    def add(self, actions):
        queue = self.get_queue()
        if queue is None:
            raise RuntimeError('Queue processor not engaged')
        queue.extend(actions)

    def sync(self):
        jar = self.context._p_jar
        if jar is not None:
            jar.sync()

    def process(self, sleep=5, once=False):
        print 'engaging'
        self.engage()
        try:
            while True:
                print 'doing processing'
                self.sync()
                executed = False
                queue = self.get_queue()
                actions = queue.popall()
                if actions is not None:
                    actions = optimize_actions(actions)
                    for action in actions:
                        print 'executing %s' % (action,)
                        action.execute()
                        executed = True
                if executed:
                    try:
                        print 'committing'
                        transaction.commit()
                    except ConflictError:
                        transaction.abort()
                else:
                    print 'no actions to execute'
                if once:
                    break
                time.sleep(sleep)
        except KeyboardInterrupt:
            pass
        finally:
            print 'disengaging'
            self.disengage()

class AddAction(Action):

    position = 2

    def __init__(self, index, mode, docid, obj):
        self.index = index
        self.mode = mode
        self.docid = docid
        self.obj = obj

    def execute(self):
        self.index.index_doc(self.docid, self.obj)
        # break all refs
        self.index = None
        self.docid = None
        self.obj = None

class ChangeAction(Action):

    position = 1
    
    def __init__(self, index, mode, docid, obj):
        self.index = index
        self.mode = mode
        self.docid = docid
        self.obj = obj

    def execute(self):
        self.index.reindex_doc(self.docid, self.obj)
        # break all refs
        self.index = None
        self.docid = None
        self.obj = None

class RemoveAction(Action):

    position = 0
    
    def __init__(self, index, mode, docid):
        self.index = index
        self.mode = mode
        self.docid = docid

    def execute(self):
        self.index.unindex_doc(self.docid)
        # break all refs
        self.index = None
        self.docid = None

class IndexActionSavepoint(object):
    """ Transaction savepoints  """

    def __init__(self, tm):
        self.tm = tm
        self.actions = tm.actions[:]

    def rollback(self):
        self.tm.actions = self.actions


@implementer(ISavepointDataManager)
class IndexActionTM(threading.local):
    # This is a data manager solely to provide savepoint support, we'd
    # otherwise be able to get away with just using a before commit hook to
    # call .process
    
    def __init__(self, index):
        self.index = index
        self.oid = index.__oid__
        self.registered = False
        self.actions = []

    def register(self):
        if not self.registered:
            t = transaction.get()
            t.join(self)
            t.addBeforeCommitHook(self.process)
            self.registered = True

    def savepoint(self):
        return IndexActionSavepoint(self)

    def tpc_begin(self, t):
        pass

    def commit(self, t):
        pass

    def tpc_vote(self, t):
        pass

    def tpc_finish(self, t):
        self.registered = False
        self.actions = []
        if self.index is not None:
            self.index.clear_action_tm()
            self.index = None # break circref

    tpc_abort = abort = tpc_finish

    def sortKey(self):
        return self.oid

    def add(self, action):
        self.actions.append(action)

    def process(self):
        if self.actions:
            actions = self.actions
            self.actions = []
            actions = optimize_actions(actions)
            self._process(actions)

    def _process(self, actions):
        registry = get_current_registry()
        processor = registry.queryAdapter(self.index, IIndexingActionProcessor)
        processor_active = processor is not None and processor.active()
        if processor_active:
            print 'action processor active'
            deferred = []
            for action in actions:
                if action.mode is MODE_DEFERRED:
                    print 'adding deferred action %r' % (action,)
                    deferred.append(action)
                else:
                    print 'executing action %r' % (action,)
                    action.execute()
            if deferred:
                processor.add(deferred)
        else:
            # if we don't have an active action processor, we must process all
            # of our actions immediately
            print 'action processor not active'
            for action in actions:
                print 'executing action %r' % (action,)
                action.execute()
        print 'done processing actions'

def optimize_actions(actions):
    """
    State chart for optimization.  If the new action is X and the existing
    action is Y, generate the resulting action named in the chart cells.

                            New    ADD      REMOVE     CHANGE

       Existing     ADD            add      nothing*   add*

                  REMOVE           change*  remove     change

                  CHANGE           add      remove     change

    Starred entries in the chart above indicate special cases.  Typically
    the last action encountered in the actions list is the most optimal
    action, except for the starred cases.
    """
    result = {}

    def donothing(docid, index_oid, action1, action2):
        del result[(index_oid, docid)]

    def doadd(docid, index_oid, action1, action2):
        result[(index_oid, docid)] = action1

    def dochange(docid, index_oid, action1, action2):
        result[(index_oid, docid)] = ChangeAction(
            action2.index, docid, action2.obj
            )

    def dodefault(docid, index_oid, action1, action2):
        result[(index_oid, docid)] = action2

    statefuncs = {
        # txn asked to remove an object that previously it was
        # asked to add, conclusion is to do nothing
        (AddAction, RemoveAction):donothing,
        # txn asked to change an object that was not previously added,
        # concusion is to just do the add
        (AddAction, ChangeAction):doadd,
        # txn action asked to remove an object then readd the same
        # object.  We translate this to a single change action.
        (RemoveAction, AddAction):dochange,
        }

    for newaction in actions:
        docid = newaction.docid
        index_oid = newaction.index.__oid__
        oldaction = result.get((index_oid, docid))
        statefunc = statefuncs.get(
            (oldaction.__class__, newaction.__class__),
            dodefault,
            )
        statefunc(docid, index_oid, oldaction, newaction)

    def sorter(action):
        return (action.index.__oid__, action.position, action.docid)

    result = list(sorted(result.values(), key=sorter))
    return result

