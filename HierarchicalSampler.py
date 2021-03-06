#!/usr/bin/env python3


from Sampling import Sampler
from sklearn.linear_model import LogisticRegression
from sklearn.cluster import AgglomerativeClustering
from scipy.sparse import vstack
from collections import defaultdict
from functools import lru_cache
import itertools
import numpy as np
import random
import logging, sys

from pprint import pprint

class Node:
    def __init__(self, node_id):
        '''Node object constructor.

        id -- integer representing id in clustering
        left -- Node object representing left child in clustering tree
        right -- Node object representing right child in clustering tree
        parent -- Node object representing parent in clustering tree
        is_leaf -- boolean representing whether node is leaf node or not
        weight -- floating point number representing weight of subtree
        subtree_leaves -- list of node objects representing the leaves in the subtree of this node
        class_to_instances -- dictionary from class --> int tallying the number of instances seen for a class
        num_revealed -- integer representing number of samples revealed that live under this node
        '''
        self.id = node_id
        self.left = None
        self.right = None
        self.parent = None

        self.is_leaf = True
        self.weight = 0
        self.subtree_leaves = []

        self.class_to_instances = defaultdict(int)
        self.num_revealed = 0



class HierarchicalSampler(Sampler):
    '''Samples datapoints based on a hierarchical method as described in the paper.
    '''
    def __init__(self, X_train, y_train, X_unlabeled, y_unlabeled, batch_size=1):
        super().__init__(X_train, y_train, X_unlabeled, y_unlabeled)
        # Hierarchical clustering portion
        self.X_merged = None
        self.y_merged = None
        self.root = None
        self.nodes = {}
        self.num_leaves = 0

        self._construct_tree()
        self._compute_weights()

        # Hierarchical sampling portion
        self.pruning = {self.root: -1}  # node_id --> majority_label
        self.labels = set(self.y_merged)
        self.revealed = set()
        self.admissible_set = set()
        self.beta = 2.0

        # "Tuning phase": exhaust all training data first
        self.tuning_phase = True
        for _ in range(X_train.shape[0]):
            self.sample()
        self.tuning_phase = False
        print('Tuning phase complete. Ready to use sampler.')

        return


    def _is_unlabeled(self, node_id):
        '''Return if node_id (corresponding to sample) is part
        of the unlabeled set.
        '''
        assert node_id < self.X_train.shape[0] + self.X_unlabeled.shape[0]
        return node_id >= self.X_train.shape[0] 

    def _construct_tree(self):
        '''Construct tree object from clusters.

        Modifies:
        self.root
        self.nodes
        self.num_leaves
        '''
        # Run clustering based on merged partition of data
        print('constructing tree')

        # Remove for production purposes
        #self.X_unlabeled = self.X_unlabeled[:150]
        #self.y_unlabeled = self.y_unlabeled[:150]

        self.X_merged = vstack([self.X_train, self.X_unlabeled]).toarray()
        self.y_merged = np.concatenate((self.y_train, self.y_unlabeled))

        # Binary tree structure
        clustering = AgglomerativeClustering()
        clustering.fit(self.X_merged)
        self.num_leaves = clustering.n_leaves_
        # INTERPRETATION: each leaf is a 1-to-1 mapping to a sample.
        assert self.num_leaves == self.X_merged.shape[0]
        ii = itertools.count(self.X_merged.shape[0])

        # Convert dictionary representation of tree into linked list tree structure
        def findNode(idx):
            return self.nodes[idx] if idx in self.nodes else Node(idx)

        #print(len(clustering.children_))
        for c in clustering.children_:
            left_id, right_id, node_id = c[0], c[1], next(ii)
            left, right, node = findNode(left_id), findNode(right_id), findNode(node_id)
            left.parent = node
            right.parent = node
            node.left = left
            node.right = right
            self.nodes[node_id] = node
            self.nodes[left_id] = left
            self.nodes[right_id] = right
        #print(len(self.nodes))

        # Mark root node and any leaf nodes
        self.root = next(node_id for node_id in self.nodes if self.nodes[node_id].parent is None)
        for node_id, node in self.nodes.items():
            if node.left or node.right:
                node.is_leaf = False

        assert type(self.root) is int
        #print(self.root)


    def _compute_weights(self):
        '''Update weights of all nodes in the tree.

        _construct_tree must have been called beforehand.
        This function should only be called once.

        Modifies:
        self.weight for all nodes in self.nodes
        '''
        
        # Helper function to process nodes in tree bottom-up
        def reverse_topological_process():
            visited = set()

            def visit(node_id):
                visited.add(node_id)
                node = self.nodes[node_id]
                if node.is_leaf:
                    node.subtree_leaves.append(node)
                else:
                    if node.left and node.left.id not in visited:
                        visit(node.left.id)
                    if node.right and node.right.id not in visited:
                        visit(node.right.id)
                    node.subtree_leaves.extend(node.left.subtree_leaves)
                    node.subtree_leaves.extend(node.right.subtree_leaves)
                node.weight = len(node.subtree_leaves)
                return

            for node_id in self.nodes:
                if node_id not in visited:
                    visit(node_id)
            return
        
        print('computing weight for all nodes in tree')

        # First find all leaf nodes in subtree
        # and set weight as number of leaf nodes that live in its subtree
        reverse_topological_process()

        # Then normalize by total number of leaves in tree
        n = 1.0 * self.num_leaves
        for node in self.nodes.values():
            node.weight /= n
            assert node.weight < n
        return


    @lru_cache(maxsize=256)
    def _get_upward_path(self, z_id, v_id):
        '''Get list of node_ids from z to v inclusive in an upward path.

        NOTE: z must be a descendant of v.
        Used in the "Update empirical counts and probabilities" portion of the code.
        '''
        trail = [z_id]
        cur = z_id
        parent = self.nodes[cur].parent.id
        while cur != v_id:
            cur = parent
            try: parent = self.nodes[cur].parent.id
            except: pass
            trail.append(cur)
            if cur is None:
                raise ValueError('broken parent in node_id {}'.format(cur)) 
        return trail


        

    def _select(self):
        '''select(P) procedure in paper.

        Selects a node from the current pruning P
        '''

        def method_1():
            '''Return node_id v in Pruning.
            
            (1) choose v in Pruning with probability proportional to w_v.
            This is similar to random sampling.'''
            # Normalize weights
            pruning = list(self.pruning.keys())
            weights = [self.nodes[node_id].weight for node_id in pruning]
            v = np.random.choice(pruning, size=None, replace=True, p=weights)
            return v
        
        return method_1()


    def _pick_sample_id_from_node(self, node_id):
        '''Pick a random point (node id) z from subtree T_{node_id}.
       
        Should only be called after all training samples have been "revealed".
        Therefore, the id returned should only be an unlabled one.
        Return None if all samples under this node have been sampled.
        '''
        # Could use margin sampling to find a weighted random sample for better performance.
        # But since we're only given a few days to work on this final project, we're not doing this :D.
        # For now, follow the algorithm.
        # Adjustment for tuning phase: always prefer training data first
        sample_ids = []
        for leaf in self.nodes[node_id].subtree_leaves: 
            sample_id = leaf.id
            if sample_id not in self.revealed:
                if not self._is_unlabeled(sample_id):
                    assert self.tuning_phase
                    self.revealed.add(sample_id)
                    return sample_id
                sample_ids.append(sample_id)

        # tuning phase: MUST use training (labeled) data.
        # not tuning phase: need to retry other node.
        if self.tuning_phase or len(sample_ids) == 0:
            return None

        unlabeled_id = random.choice(sample_ids)
        self.revealed.add(unlabeled_id)
        return random.choice(sample_ids)



    def sample(self):
        '''Return selected training sample in X_unlabeled and corresponding label.

        In hierarchical sampling, this procedure should select the datapoint based
        on the rest of the unsampled data as well as the structure of the tree.
        '''

        # Helper function to update empirical counts and probabilities
        def update_counts(z_label, z_id, v_id):
            for node_id in self._get_upward_path(z_id, v_id):
                node = self.nodes[node_id]
                node.class_to_instances[z_label] += 1
                node.num_revealed += 1
            return

        v_id, z_id = -1, -1  # sentinel values
        while True:
            # Loop section begin: keep looping because node v may have 
            # all its associated samples revealed already.
            v_id = self._select()
            z_id = self._pick_sample_id_from_node(v_id)

            if z_id is None:
                print("finding another node to draw sanmples from")
                continue
            # Loop section end.
            # Valid z when reach here
            print('z: '+str(z_id))
            z_label = self.y_merged[z_id]
            update_counts(z_label, z_id, v_id)
            self._update(self._get_upward_path(z_id, v_id), [z_label])
            break
        # Return sample when actually using sampler
        if not self.tuning_phase:
            return self.X_merged[z_id], self.y_merged[z_id]

    def _update(self, update_nodes, update_labels):
        '''Update. 

        update_nodes -- list of nodes to update the admissible set with.
        This is typically the upward path of the datapoint just sampled.

        update_labels -- list of labels to update the admissible set with.
        This is typically just a single label for the datapoint just sampled.
        '''
        p_vl = {}  # (node_id, int label) --> float
        for node_id, node in self.nodes.items():
            for label in self.labels:
                p_vl[(node_id,label)] = 0
                if label in node.class_to_instances:
                    class_revealed_count = node.class_to_instances[label]
                    p_vl[(node_id,label)] = class_revealed_count / (1.0*node.num_revealed)

        delta = {}
        for vl in p_vl:
            v_id, label = vl
            delta[vl] = 0
            n_v = self.nodes[v_id].num_revealed 
            if n_v > 0:
                delta[vl] = 1/n_v + (p_vl[vl] * (1-p_vl[vl]) / n_v)**0.5

        # Math functions here
        def p_LB(v_id, label):
            # no observations
            if len(self.nodes[v_id].class_to_instances) == 0:
                return 0
            vl = (v_id, label)
            return max(p_vl[vl]-delta[vl] , 0.0)
        def p_UB(v_id, label):
            # no observations
            if len(self.nodes[v_id].class_to_instances) == 0:
                return 1
            vl = (v_id, label)
            return min(p_vl[vl]+delta[vl], 1.0)

        p_vl_LB = {(node_id, label): p_LB(node_id, label) for (node_id, label) in p_vl}
        p_vl_UB = {(node_id, label): p_UB(node_id, label) for (node_id, label) in p_vl}

        # Update admissible set A
        def is_admissible(v_id, label):
            vl = (v_id, label)
            lb = p_vl_LB[vl]
            min_other = float('inf')
            for lp in self.labels:
                if lp != label:
                    vlp = (v_id, lp)
                    min_other = min(min_other, 1 - p_vl_UB[vlp])
            #print('1 - '+str(lb)+' < 2 * '+str(min_other))
            return 1 - lb < self.beta * min_other

        for node_id in update_nodes:
            for label in update_labels:
                if is_admissible(node_id, label):
                    #print('node ' + str(node_id) + ' label '+str(label) + ' is admissible')
                    self.admissible_set.add((node_id, label))


        # Compute epsilon_tilda_vl and s_v values bottom-up
        epsilon_tilda_vl = {}  # (node_id, int label) --> float
        s_v = {}  # node_id --> [score, P' and L' represented as {v_id: label}]
        # Helper function to process nodes in tree bottom-up
        def bottom_up_compute():
            visited = set()

            def visit(node_id):
                '''Mutates entries in epsilon_tilda_vl and s_v.'''
                visited.add(node_id)

                node = self.nodes[node_id]

                if node.left and node.left.id not in visited:
                    visit(node.left.id)
                if node.right and node.right.id not in visited:
                    visit(node.right.id)

                # All children have been visited
                has_admissible = False
                for label in self.labels:
                    vl = (node_id, label)
                    if vl in self.admissible_set:
                        has_admissible = True
                        epsilon_tilda_vl[vl] = 1 - p_vl[vl]
                    else:
                        epsilon_tilda_vl[vl] = 1
                best_label = min((l for l in self.labels), key=lambda l: epsilon_tilda_vl[(node_id,l)])
                best_score = epsilon_tilda_vl[(node_id, best_label)]
                s_v[node_id] = [best_score, {node_id: best_label}]
                if has_admissible and not node.is_leaf:
                    w_v = node.weight
                    children_score, children_pruning = 0, {}
                    
                    if node.left:
                        left_id = node.left.id
                        left_score, left_pruning = s_v[left_id]
                        #print('node.left: ' + str(node.left.weight / w_v * left_score))
                        children_score += node.left.weight / w_v * left_score
                        children_pruning.update(left_pruning)
                    if node.right:
                        right_id = node.right.id
                        right_score, right_pruning = s_v[right_id]
                        #print('node.right: ' + str(node.right.weight / w_v * right_score))
                        children_score += node.right.weight / w_v * right_score
                        children_pruning.update(right_pruning)
                    
                    if children_score < best_score:
                        s_v[node_id] = [children_score, children_pruning]
                return

            for node_id in self.nodes:
                if node_id not in visited:
                    visit(node_id)
            return

        bottom_up_compute()

        original_pruning = list(self.pruning.keys())
        for v_id in original_pruning:
            score, PL_prime = s_v[v_id]
            del self.pruning[v_id]
            self.pruning.update(PL_prime)
        #pprint(self.pruning)

        return
        

if __name__ == '__main__':
    from sklearn.datasets import fetch_20newsgroups_vectorized
    from sklearn.model_selection import train_test_split
    from pprint import pprint

    training_size = 100
    max_unlabeled_size = 500

    dataset = fetch_20newsgroups_vectorized(subset='train')
    X_train_base = dataset.data
    y_train_base = dataset.target
    X_train, y_train = X_train_base[:training_size], y_train_base[:training_size]
    X_unlabeled, y_unlabeled = X_train_base[training_size:], y_train_base[training_size:]

    hs = HierarchicalSampler(X_train, y_train, X_unlabeled, y_unlabeled)
    print(hs.sample())
