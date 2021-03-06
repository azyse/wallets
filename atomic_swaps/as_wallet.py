from standard_wallet.wallet import Wallet
import clvm
from chiasim.hashable import CoinSolution, Program, ProgramHash, SpendBundle
from clvm_tools import binutils
from chiasim.validation.Conditions import ConditionOpcode
from chiasim.puzzles.p2_delegated_puzzle import puzzle_for_pk
from utilities.puzzle_utilities import puzzlehash_from_string
from utilities.keys import signature_for_solution, sign_f_for_keychain


def build_spend_bundle(coin, solution, sign_f):
    coin_solution = CoinSolution(coin, solution)
    signature = signature_for_solution(solution, sign_f)
    return SpendBundle([coin_solution], signature)


# ASWallet is subclass of Wallet
class ASWallet(Wallet):
    def __init__(self):
        self.as_pending_utxos = set()
        self.overlook = []
        self.as_swap_list = []
        super().__init__()
        return

    def notify(self, additions, deletions):
        super().notify(additions, deletions)
        puzzlehashes = []
        for swap in self.as_swap_list:
            puzzlehashes.append(swap["outgoing puzzlehash"])
            puzzlehashes.append(swap["incoming puzzlehash"])
        if puzzlehashes != []:
            self.as_notify(additions, puzzlehashes)

    def as_notify(self, additions, puzzlehashes):
        for coin in additions:
            for puzzlehash in puzzlehashes:
                if coin.puzzle_hash.hex() == puzzlehash and coin.puzzle_hash not in self.overlook:
                    self.as_pending_utxos.add(coin)
                    self.overlook.append(coin.puzzle_hash)

    # finds a pending atomic swap coin to be spent
    def as_select_coins(self, amount, as_puzzlehash):
        if amount > self.current_balance or amount < 0:
            return None
        used_utxos = set()
        if isinstance(as_puzzlehash, str):
            as_puzzlehash = puzzlehash_from_string(as_puzzlehash)
        coins = self.my_utxos.copy()
        for pcoin in self.as_pending_utxos:
            coins.add(pcoin)
        for coin in coins:
            if coin.puzzle_hash == as_puzzlehash:
                used_utxos.add(coin)
        return used_utxos

    # generates the hash of the secret used for the atomic swap coin hashlocks
    def as_generate_secret_hash(self, secret):
        secret_hash_cl = f"(sha256 (q {secret}))"
        sec = f"({secret})"
        cost, secret_hash_preformat = clvm.run_program(binutils.assemble("(sha256 (f (a)))"), binutils.assemble(sec))
        secret_hash = binutils.disassemble(secret_hash_preformat)
        return secret_hash

    def as_make_puzzle(self, as_pubkey_sender, as_pubkey_receiver, as_amount, as_timelock_block, as_secret_hash):
        as_pubkey_sender_cl = f"0x{as_pubkey_sender.hex()}"
        as_pubkey_receiver_cl = f"0x{as_pubkey_receiver.hex()}"
        as_payout_puzzlehash_receiver = ProgramHash(puzzle_for_pk(as_pubkey_receiver))
        as_payout_puzzlehash_sender = ProgramHash(puzzle_for_pk(as_pubkey_sender))
        payout_receiver = f"(c (q 0x{ConditionOpcode.CREATE_COIN.hex()}) (c (q 0x{as_payout_puzzlehash_receiver.hex()}) (c (q {as_amount}) (q ()))))"
        payout_sender = f"(c (q 0x{ConditionOpcode.CREATE_COIN.hex()}) (c (q 0x{as_payout_puzzlehash_sender.hex()}) (c (q {as_amount}) (q ()))))"
        aggsig_receiver = f"(c (q 0x{ConditionOpcode.AGG_SIG.hex()}) (c (q {as_pubkey_receiver_cl}) (c (sha256tree (a)) (q ()))))"
        aggsig_sender = f"(c (q 0x{ConditionOpcode.AGG_SIG.hex()}) (c (q {as_pubkey_sender_cl}) (c (sha256tree (a)) (q ()))))"
        receiver_puz = (f"((c (i (= (sha256 (f (r (a)))) (q {as_secret_hash})) (q (c " + aggsig_receiver + " (c " + payout_receiver + " (q ())))) (q (x (q 'invalid secret')))) (a))) ) ")
        timelock = f"(c (q 0x{ConditionOpcode.ASSERT_BLOCK_INDEX_EXCEEDS.hex()}) (c (q {as_timelock_block}) (q ()))) "
        sender_puz = "(c " + aggsig_sender + " (c " + timelock + " (c " + payout_sender + " (q ()))))"
        as_puz_sender = "((c (i (= (f (a)) (q 77777)) (q " + sender_puz + ") (q (x (q 'not a valid option'))) ) (a)))"
        as_puz = "((c (i (= (f (a)) (q 33333)) (q " + receiver_puz + " (q " + as_puz_sender + ")) (a)))"
        return Program(binutils.assemble(as_puz))

    def as_get_new_puzzlehash(self, as_pubkey_sender, as_pubkey_receiver, as_amount, as_timelock_block, as_secret_hash):
        as_puz = self.as_make_puzzle(as_pubkey_sender, as_pubkey_receiver, as_amount, as_timelock_block, as_secret_hash)
        as_puzzlehash = ProgramHash(as_puz)
        return as_puzzlehash

    # 33333 is the receiver solution code prefix
    def as_make_solution_receiver(self, as_sec_to_try):
        sol = "(33333 "
        sol += f"{as_sec_to_try}"
        sol += ")"
        return Program(binutils.assemble(sol))

    # 77777 is the sender solution code prefix
    def as_make_solution_sender(self):
        sol = "(77777 "
        sol += ")"
        return Program(binutils.assemble(sol))

    # finds the secret used to spend a swap coin so that it can be used to spend the swap's other coin
    def pull_preimage(self, body, removals):
        for coin in removals:
            for swap in self.as_swap_list:
                if coin.puzzle_hash.hex() == swap["outgoing puzzlehash"]:
                    l = [(puzzle_hash, puzzle_solution_program) for (puzzle_hash, puzzle_solution_program) in self.as_solution_list(body.solution_program)]
                    for x in l:
                        if x[0].hex() == coin.puzzle_hash.hex():
                            pre1 = binutils.disassemble(x[1])
                            preimage = pre1[(len(pre1) - 515):(len(pre1) - 3)]
                            swap["secret"] = preimage

    # returns a list of tuples of the form (coin_name, puzzle_hash, conditions_dict, puzzle_solution_program)
    def as_solution_list(self, body_program):
        try:
            cost, sexp = clvm.run_program(body_program, [])
        except clvm.EvalError.EvalError:
            raise ValueError(body_program)
        npc_list = []
        for name_solution in sexp.as_iter():
            _ = name_solution.as_python()
            if len(_) != 2:
                raise ValueError(name_solution)
            if not isinstance(_[0], bytes) or len(_[0]) != 32:
                raise ValueError(name_solution)
            if not isinstance(_[1], list) or len(_[1]) != 2:
                raise ValueError(name_solution)
            puzzle_solution_program = name_solution.rest().first()
            puzzle_program = puzzle_solution_program.first()
            puzzle_hash = ProgramHash(Program(puzzle_program))
            npc_list.append((puzzle_hash, puzzle_solution_program))
        return npc_list

    def get_private_keys(self):
        return [self.extended_secret_key.private_child(child) for child in range(self.next_address)]

    def make_keychain(self):
        private_keys = self.get_private_keys()
        return dict((_.public_key(), _) for _ in private_keys)

    def make_signer(self):
        return sign_f_for_keychain(self.make_keychain())

    def as_create_spend_bundle(self, as_puzzlehash, as_amount, as_timelock_block, as_secret_hash, as_pubkey_sender = None, as_pubkey_receiver = None, who = None, as_sec_to_try = None):
        utxos = self.as_select_coins(as_amount, as_puzzlehash)
        spends = []
        for coin in utxos:
            puzzle = self.as_make_puzzle(as_pubkey_sender, as_pubkey_receiver, as_amount, as_timelock_block, as_secret_hash)
            if who == "sender":
                solution = self.as_make_solution_sender()
            elif who == "receiver":
                solution = self.as_make_solution_receiver(as_sec_to_try)
            pair = solution.to([puzzle, solution])
            signer = self.make_signer()
            spend_bundle = build_spend_bundle(coin, Program(pair), sign_f=signer)
            spends.append(spend_bundle)
        return SpendBundle.aggregate(spends)

    def as_remove_swap_instances(self, removals):
        for coin in removals:
            pcoins = self.as_pending_utxos.copy()
            for pcoin in pcoins:
                if coin.puzzle_hash == pcoin.puzzle_hash:
                    self.as_pending_utxos.remove(pcoin)
            for swap in self.as_swap_list:
                if coin.puzzle_hash.hex() == swap["outgoing puzzlehash"]:
                    swap["outgoing puzzlehash"] = "spent"
                    if swap["outgoing puzzlehash"] == "spent" and swap["incoming puzzlehash"] == "spent":
                        self.as_swap_list.remove(swap)
                if coin.puzzle_hash.hex() == swap["incoming puzzlehash"]:
                    swap["incoming puzzlehash"] = "spent"
                    if swap["outgoing puzzlehash"] == "spent" and swap["incoming puzzlehash"] == "spent":
                        self.as_swap_list.remove(swap)


"""
Copyright 2018 Chia Network Inc
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
   http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
